"""Deterministic content-type detection + a universal smart-compress router.

Stdlib only (re, json). This is the *safe routing* layer that sits in front of
the compressors in ``simplicio_compress`` / ``simplicio_compress_extra``: it
classifies a piece of content (JSON / CODE / LOG / MARKDOWN / PROSE) with cheap
deterministic heuristics, then routes it to the BEST available lossless
compressor for that type.

Content-type categories for routing compression, paired with a universal-router
idea, using rule-based detection (no ML / Magika dependency) so it stays
stdlib-only and offline.

Public API:
    ContentType            -- str constants: json, code, log, markdown, prose
    detect(text)           -> (content_type, confidence, meta)
    universal_compress(text) -> (compressed, info)

Routing (lossless / conservative — NOT the lossy ML pruning layer):
    json              -> JSON minify (simplicio_compress.minify_json, else json.dumps)
    log / markdown /  -> full simplicio_compress.compress pipeline
        prose            + simplicio_compress_extra.compress_extra if present
    code              -> left intact (only whitespace-safe base pipeline, no-op
                         on real code unless it has trailing ws / ANSI)

Degrades gracefully: siblings are imported with a sys.path insert; if they are
missing, falls back to a trivial whitespace/dedup compressor. Nothing here is
lossy and code is never reformatted.
"""

from __future__ import annotations

import json
import os
import re
import sys

__all__ = ["ContentType", "detect", "universal_compress"]


class ContentType:
    """High-level content categories for compression routing (str constants)."""

    JSON = "json"
    CODE = "code"
    LOG = "log"
    MARKDOWN = "markdown"
    PROSE = "prose"


# --------------------------------------------------------------------------- #
# Detection signals (deterministic, line-based heuristics).
# --------------------------------------------------------------------------- #

# Per-language keyword sets — presence drives the detected `language` in meta.
_LANG_KEYWORDS = {
    "python": (
        r"\bdef\b", r"\bclass\b", r"\bimport\b", r"\bfrom\b\s+\w", r"\belif\b",
        r"\bself\b", r"\blambda\b", r"\bprint\(", r":\s*$",
    ),
    "javascript": (
        r"\bfunction\b", r"\bconst\b", r"\blet\b", r"\bvar\b", r"=>",
        r"\bconsole\.\w", r"\brequire\(", r"\bexport\b", r";\s*$",
    ),
    "typescript": (
        r"\binterface\b", r"\btype\s+\w+\s*=", r":\s*(?:string|number|boolean)\b",
        r"\bexport\b", r"\bimport\b.*\bfrom\b", r"=>",
    ),
    "go": (
        r"\bfunc\b", r"\bpackage\b", r"\bimport\b", r":=", r"\bdefer\b",
        r"\bchan\b", r"\bgo\s+\w",
    ),
    "java": (
        r"\bpublic\b", r"\bprivate\b", r"\bclass\b", r"\bvoid\b", r"\bstatic\b",
        r"\bimport\b", r"\bnew\b", r";\s*$",
    ),
    "c": (
        r"#include\b", r"\bint\s+main\b", r"\bprintf\(", r"\bvoid\b",
        r"\bstruct\b", r";\s*$",
    ),
    "shell": (
        r"^#!.*\bsh\b", r"\becho\b", r"\bif\s+\[", r"\bfi\b", r"\bdone\b",
        r"\$\{?\w", r"\bexport\b",
    ),
    "sql": (
        r"\bSELECT\b", r"\bFROM\b", r"\bWHERE\b", r"\bINSERT\b", r"\bUPDATE\b",
        r"\bCREATE\s+TABLE\b", r"\bJOIN\b",
    ),
}

# Generic "this is code" structural signals (independent of language).
_CODE_BRACKETS = re.compile(r"[{}();]")
_CODE_INDENT = re.compile(r"^[ \t]+\S", re.MULTILINE)
_CODE_ASSIGN = re.compile(r"[\w\]\)]\s*=\s*\S|=>|:=|==|!=|<=|>=")

# Log signals: ISO timestamp, HH:MM:SS, bracketed tag, or a log level token.
_LOG_TS = re.compile(
    r"^\s*(?:"
    r"\[[^\]]+\]"                                   # [tag] / [2024-...]
    r"|\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"       # ISO ts
    r"|\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"               # HH:MM:SS
    r")"
)
_LOG_LEVEL = re.compile(
    r"\b(?:TRACE|DEBUG|INFO|INFORMATION|WARN|WARNING|ERROR|ERR|FATAL|CRITICAL)\b"
)

# Markdown signals: ATX headings, fenced code, tables, lists, blockquotes.
_MD_HEADING = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)
_MD_FENCE = re.compile(r"^```|^~~~", re.MULTILINE)
_MD_TABLE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_MD_LIST = re.compile(r"^\s*(?:[-*+]\s+|\d+\.\s+)\S", re.MULTILINE)
_MD_QUOTE = re.compile(r"^\s*>\s+\S", re.MULTILINE)
_MD_LINKIMG = re.compile(r"!?\[[^\]]+\]\([^)]+\)")


def _nonblank_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip()]


def _looks_like_json(text: str) -> "tuple[bool, dict]":
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return False, {}
    try:
        obj = json.loads(stripped)
    except (ValueError, TypeError):
        return False, {}
    if not isinstance(obj, (dict, list)):
        return False, {}
    kind = "object" if isinstance(obj, dict) else "array"
    size = len(obj)
    return True, {"json_kind": kind, "json_size": size}


def _detect_language(text: str) -> "tuple[str | None, float]":
    """Return (best language, hit-fraction) by keyword voting."""
    best_lang = None
    best_score = 0
    best_total = 1
    for lang, patterns in _LANG_KEYWORDS.items():
        hits = 0
        for pat in patterns:
            if re.search(pat, text, re.MULTILINE):
                hits += 1
        if hits > best_score:
            best_score = hits
            best_lang = lang
            best_total = len(patterns)
    if best_lang is None or best_score == 0:
        return None, 0.0
    return best_lang, best_score / best_total


def _code_structure_score(text: str, lines: list[str]) -> float:
    """0..1 — density of code-shaped structure (brackets / indent / assigns)."""
    if not lines:
        return 0.0
    n = len(text) or 1
    bracket_density = len(_CODE_BRACKETS.findall(text)) / n
    indented = len(_CODE_INDENT.findall(text))
    indent_frac = indented / len(lines)
    assigns = len(_CODE_ASSIGN.findall(text))
    assign_frac = min(1.0, assigns / max(1, len(lines)))
    # Weighted blend; bracket density is scaled up since it's small per-char.
    score = (
        min(1.0, bracket_density * 25.0) * 0.45
        + indent_frac * 0.35
        + assign_frac * 0.20
    )
    return min(1.0, score)


def detect(text: str) -> "tuple[str, float, dict]":
    """Classify `text` into a ContentType.

    Returns ``(content_type, confidence, meta)`` where ``content_type`` is one
    of the ``ContentType`` constants, ``confidence`` is a 0..1 float, and
    ``meta`` may carry signal details (e.g. detected code ``language``).

    Detection order: JSON first (parse / leading bracket),
    then CODE, LOG, MARKDOWN, else PROSE. Deterministic for a given input.
    """
    meta: dict = {}
    if not isinstance(text, str) or not text.strip():
        meta["reason"] = "empty"
        return ContentType.PROSE, 0.0, meta

    # 1. JSON — authoritative when the whole blob parses as a JSON value.
    is_json, jmeta = _looks_like_json(text)
    if is_json:
        meta.update(jmeta)
        meta["reason"] = "parsed_json"
        return ContentType.JSON, 1.0, meta

    lines = _nonblank_lines(text)
    total = len(lines) or 1

    # 2. CODE — strong language keyword match OR dense code structure.
    language, lang_frac = _detect_language(text)
    struct = _code_structure_score(text, lines)
    if language is not None:
        meta["language"] = language
    meta["lang_frac"] = round(lang_frac, 3)
    meta["struct"] = round(struct, 3)

    # 3. LOG — fraction of lines that begin with a timestamp/tag, or carry a
    #    log level. Computed early so we can prefer LOG over CODE for log dumps.
    ts_lines = sum(1 for ln in lines if _LOG_TS.match(ln))
    level_lines = sum(1 for ln in lines if _LOG_LEVEL.search(ln))
    ts_frac = ts_lines / total
    level_frac = level_lines / total
    meta["log_ts_frac"] = round(ts_frac, 3)
    meta["log_level_frac"] = round(level_frac, 3)

    # 4. MARKDOWN — count distinct markdown marker kinds present.
    md_kinds = 0
    md_heading = bool(_MD_HEADING.search(text))
    md_fence = bool(_MD_FENCE.search(text))
    md_table = bool(_MD_TABLE.search(text))
    md_list = bool(_MD_LIST.search(text))
    md_quote = bool(_MD_QUOTE.search(text))
    md_link = bool(_MD_LINKIMG.search(text))
    for present in (md_heading, md_fence, md_table, md_list, md_quote, md_link):
        if present:
            md_kinds += 1
    meta["md_kinds"] = md_kinds

    # --- Decide. LOG wins over CODE/MD when timestamps dominate. ---
    log_conf = max(ts_frac, 0.6 * ts_frac + 0.6 * level_frac)
    if ts_frac >= 0.5 or (ts_frac >= 0.3 and level_frac >= 0.3):
        meta["reason"] = "timestamped_lines"
        return ContentType.LOG, min(1.0, max(0.6, log_conf)), meta

    # CODE — clear language signal or dense structure.
    if (language is not None and lang_frac >= 0.35) or struct >= 0.5:
        conf = max(lang_frac, struct)
        meta["reason"] = "code_keywords_or_structure"
        return ContentType.CODE, min(1.0, max(0.55, conf)), meta

    # MARKDOWN — a heading/fence alone is decisive; otherwise 2+ marker kinds.
    if md_heading or md_fence or md_kinds >= 2:
        conf = 0.9 if (md_heading or md_fence) else 0.6 + 0.1 * md_kinds
        meta["reason"] = "markdown_markers"
        return ContentType.MARKDOWN, min(1.0, conf), meta

    # Weak log evidence (no timestamps but many log levels) -> LOG.
    if level_frac >= 0.5:
        meta["reason"] = "log_levels"
        return ContentType.LOG, min(1.0, max(0.55, level_frac)), meta

    # Weak code evidence falls through to PROSE unless structure is moderate.
    if struct >= 0.35:
        meta["reason"] = "moderate_code_structure"
        return ContentType.CODE, max(0.5, struct), meta

    # 5. PROSE — default.
    meta["reason"] = "default_prose"
    # Confidence: higher when there's little machine structure.
    prose_conf = max(0.5, 1.0 - max(struct, ts_frac, level_frac, md_kinds / 6))
    return ContentType.PROSE, min(1.0, prose_conf), meta


# --------------------------------------------------------------------------- #
# Universal compressor — route to the best AVAILABLE lossless compressor.
# --------------------------------------------------------------------------- #


def _import_siblings():
    """Import sibling compressors with a sys.path insert; degrade gracefully.

    Returns ``(compress_fn, compress_extra_fn, minify_json_fn)`` where any
    member may be ``None`` if its module is absent.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)

    compress_fn = None
    extra_fn = None
    minify_fn = None
    try:
        import simplicio_compress as _sc  # type: ignore

        compress_fn = getattr(_sc, "compress", None)
        minify_fn = getattr(_sc, "minify_json", None)
    except Exception:
        pass
    try:
        import simplicio_compress_extra as _sce  # type: ignore

        extra_fn = getattr(_sce, "compress_extra", None)
    except Exception:
        pass
    return compress_fn, extra_fn, minify_fn


# Trivial fallback compressor (used only when siblings are unavailable). Same
# safety contract: whitespace + consecutive-dup only, shrink-only, no-op on code
# beyond trailing whitespace.
_TRAILING_WS = re.compile(r"[ \t]+(?=\r?\n)|[ \t]+\Z")
_BLANK_RUN = re.compile(r"(?:[ \t]*\r?\n){4,}")


def _fallback_compress(text: str) -> str:
    out = _TRAILING_WS.sub("", text)

    def _blanks(m: "re.Match[str]") -> str:
        eol = "\r\n" if "\r\n" in m.group(0) else "\n"
        return eol * 3

    out = _BLANK_RUN.sub(_blanks, out)
    # collapse consecutive identical non-blank lines
    lines = out.splitlines(keepends=True)
    deduped: list[str] = []
    prev = None
    for ln in lines:
        if ln == prev and ln.strip():
            continue
        deduped.append(ln)
        prev = ln
    out = "".join(deduped)
    return out if len(out) < len(text) else text


def _minify_json_safe(text: str, minify_fn) -> "tuple[str, str]":
    """Return (minified, technique). Falls back to json.dumps if no sibling."""
    if minify_fn is not None:
        try:
            out = minify_fn(text)
            if isinstance(out, str) and len(out) <= len(text):
                return out, "simplicio_compress.minify_json"
        except Exception:
            pass
    # direct stdlib minify
    stripped = text.strip()
    if stripped and stripped[0] in "{[":
        try:
            obj = json.loads(stripped)
            if isinstance(obj, (dict, list)):
                out = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
                if len(out) <= len(text):
                    return out, "json.dumps_minify"
        except (ValueError, TypeError):
            pass
    return text, "noop"


def universal_compress(text: str) -> "tuple[str, dict]":
    """Detect `text`'s type, then route it to the best lossless compressor.

    Returns ``(compressed, info)`` with
    ``info = {content_type, technique, before, after, pct}``. The pass is always
    lossless and conservative — code is never reformatted, only whitespace-safe
    passes touch it. Degrades to a trivial compressor when siblings are missing.
    """
    if not isinstance(text, str):
        text = str(text)
    before = len(text)
    content_type, _conf, _meta = detect(text)
    compress_fn, extra_fn, minify_fn = _import_siblings()

    techniques: list[str] = []
    out = text

    if content_type == ContentType.JSON:
        out, tech = _minify_json_safe(text, minify_fn)
        techniques.append(tech)
    else:
        # log / markdown / prose / code -> base pipeline (+ extra if present).
        # The base pipeline is whitespace/dedup/fold only, so it is safe on code
        # too (it won't reformat it; at most strips trailing ws / ANSI).
        if compress_fn is not None:
            try:
                piped = compress_fn(out)
                if isinstance(piped, str) and len(piped) <= len(out):
                    out = piped
                    techniques.append("simplicio_compress.compress")
            except Exception:
                pass
        else:
            piped = _fallback_compress(out)
            if len(piped) < len(out):
                out = piped
                techniques.append("fallback_compress")

        if extra_fn is not None:
            try:
                piped = extra_fn(out)
                if isinstance(piped, str) and len(piped) <= len(out):
                    out = piped
                    techniques.append("simplicio_compress_extra.compress_extra")
            except Exception:
                pass

    # Never grow the input.
    if len(out) > before:
        out = text
        techniques = ["noop"]
    if not techniques or len(out) == before:
        if not techniques:
            techniques = ["noop"]

    after = len(out)
    saved = before - after
    pct = round((saved / before) * 100, 2) if before else 0.0
    info = {
        "content_type": content_type,
        "technique": "+".join(techniques) if techniques else "noop",
        "before": before,
        "after": after,
        "pct": pct,
    }
    return out, info


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _main(argv: "list[str]") -> int:
    args = list(argv)
    as_json = False
    if "--json" in args:
        as_json = True
        args.remove("--json")

    cmd = args[0] if args else ""
    if cmd not in ("detect", "compress"):
        sys.stderr.write(
            "usage: simplicio_detect.py {detect|compress} [--json]  (reads stdin)\n"
        )
        return 2

    data = sys.stdin.read()

    if cmd == "detect":
        ctype, conf, meta = detect(data)
        if as_json:
            sys.stdout.write(
                json.dumps(
                    {"content_type": ctype, "confidence": round(conf, 4), "meta": meta},
                    ensure_ascii=False,
                )
                + "\n"
            )
        else:
            sys.stdout.write("%s\t%.3f\n" % (ctype, conf))
        return 0

    # compress
    out, info = universal_compress(data)
    if as_json:
        sys.stderr.write(json.dumps(info, ensure_ascii=False) + "\n")
        sys.stdout.write(out)
        if out and not out.endswith("\n"):
            sys.stdout.write("\n")
    else:
        sys.stdout.write(out)
        if out and not out.endswith("\n"):
            sys.stdout.write("\n")
        sys.stderr.write(
            "[%s] technique=%s before=%d after=%d pct=%s%%\n"
            % (
                info["content_type"],
                info["technique"],
                info["before"],
                info["after"],
                info["pct"],
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
