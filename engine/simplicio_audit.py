"""audit-reads — scan files/dirs and estimate token savings from compression.

Audit-reads: walk the given paths, run the deterministic
`simplicio_compress` pipeline over each readable text file, and rank files by how
many tokens compression would save (tokens ~= chars / 4). Helps decide which
read-side context is worth compressing before it floods the LLM.

Stdlib only (os, sys, json, argparse, pathlib). No network.

Usage:
    python3 simplicio_audit.py <path> [<path> ...] [--top N] [--json] [--min-bytes N]

For each text file it prints: file, before->after chars, %saved, ~tokens saved,
and the top (most-impactful) algorithm. A TOTAL line sums potential token
savings across all scanned files.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Make the sibling engine modules importable regardless of cwd.
_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)

try:
    from simplicio_compress import compress, compress_report  # noqa: F401
except Exception:  # pragma: no cover - fallback only when the import breaks.
    # Trivial whitespace/dedup fallback so the auditor still runs standalone.
    def compress(text):  # type: ignore[misc]
        if not isinstance(text, str) or not text:
            return text
        lines = text.splitlines(keepends=True)
        out = []
        prev = None
        for line in lines:
            stripped = line.rstrip()
            stripped += line[len(line.rstrip("\r\n")):]  # keep EOL
            if stripped == prev and stripped.strip() != "":
                continue
            out.append(stripped)
            prev = stripped
        joined = "".join(out)
        return joined if len(joined) < len(text) else text

    def compress_report(text):  # type: ignore[misc]
        if not isinstance(text, str):
            text = str(text)
        before = len(text)
        out = compress(text)
        after = len(out)
        saved = before - after
        pct = round((saved / before) * 100, 2) if before else 0.0
        return {
            "before": before,
            "after": after,
            "saved": saved,
            "pct": pct,
            "applied": ["whitespace_dedup"] if saved else [],
        }


MAX_FILE_BYTES = 2 * 1024 * 1024  # skip files larger than 2MB
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}
CHARS_PER_TOKEN = 4


def _is_probably_binary(raw: bytes) -> bool:
    """Heuristic: NUL byte or a high ratio of non-text bytes => binary."""
    if b"\x00" in raw:
        return True
    sample = raw[:4096]
    if not sample:
        return False
    text_chars = bytes(range(0x20, 0x7F)) + b"\n\r\t\f\b"
    nontext = sum(1 for b in sample if b not in text_chars)
    return (nontext / len(sample)) > 0.30


def _iter_files(paths):
    """Yield Path objects for each candidate file under the given paths."""
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for root, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for name in sorted(files):
                    yield Path(root) / name
        elif p.is_file():
            yield p
        # non-existent paths fall through silently; reported as skipped below.


def _top_algo(applied):
    """The most-impactful algo is the first one the pipeline applied."""
    return applied[0] if applied else "-"


def audit(paths, min_bytes=0):
    """Return (rows, skipped) where rows are sorted by tokens_saved desc.

    Each row: dict(path, before, after, saved, pct, tokens_saved, top_algo).
    skipped: list of (path, reason).
    """
    rows = []
    skipped = []
    seen = set()
    for fp in _iter_files(paths):
        rp = str(fp)
        if rp in seen:
            continue
        seen.add(rp)
        try:
            size = fp.stat().st_size
        except OSError as exc:
            skipped.append((rp, "stat failed: %s" % exc.__class__.__name__))
            continue
        if size > MAX_FILE_BYTES:
            skipped.append((rp, "too large (%d bytes)" % size))
            continue
        if size < min_bytes:
            skipped.append((rp, "below --min-bytes"))
            continue
        try:
            raw = fp.read_bytes()
        except OSError as exc:
            skipped.append((rp, "unreadable: %s" % exc.__class__.__name__))
            continue
        if _is_probably_binary(raw):
            skipped.append((rp, "binary"))
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("latin-1")
            except Exception:
                skipped.append((rp, "undecodable"))
                continue
        rep = compress_report(text)
        tokens_saved = rep["saved"] // CHARS_PER_TOKEN
        rows.append(
            {
                "path": rp,
                "before": rep["before"],
                "after": rep["after"],
                "saved": rep["saved"],
                "pct": rep["pct"],
                "tokens_saved": tokens_saved,
                "top_algo": _top_algo(rep["applied"]),
                "applied": rep["applied"],
            }
        )
    rows.sort(key=lambda r: (r["tokens_saved"], r["saved"]), reverse=True)
    return rows, skipped


def _shorten(path, width=48):
    if len(path) <= width:
        return path
    return "…" + path[-(width - 1):]


def render_table(rows, skipped, top=None):
    """Build the human-readable ranked table as a string."""
    lines = []
    shown = rows if top is None else rows[:top]
    if not rows:
        lines.append("no compressible files found")
        for path, reason in skipped:
            lines.append("  skip %s (%s)" % (_shorten(path), reason))
        return "\n".join(lines)

    header = "%-48s %15s %7s %10s  %s" % (
        "FILE", "BEFORE->AFTER", "%SAVED", "~TOK SAVED", "TOP ALGO"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for r in shown:
        ba = "%d->%d" % (r["before"], r["after"])
        lines.append(
            "%-48s %15s %6.1f%% %10d  %s"
            % (_shorten(r["path"]), ba, r["pct"], r["tokens_saved"], r["top_algo"])
        )
    if top is not None and len(rows) > top:
        lines.append("… (%d more files not shown)" % (len(rows) - top))

    total_tokens = sum(r["tokens_saved"] for r in rows)
    total_saved = sum(r["saved"] for r in rows)
    lines.append("-" * len(header))
    lines.append(
        "TOTAL: %d files, %d chars saved, ~%d tokens saved"
        % (len(rows), total_saved, total_tokens)
    )
    if skipped:
        lines.append("(%d files skipped)" % len(skipped))
    return "\n".join(lines)


def build_json(rows, skipped, top=None):
    shown = rows if top is None else rows[:top]
    return {
        "files": [
            {
                "path": r["path"],
                "before": r["before"],
                "after": r["after"],
                "saved": r["saved"],
                "pct": r["pct"],
                "tokens_saved": r["tokens_saved"],
                "top_algo": r["top_algo"],
                "applied": r["applied"],
            }
            for r in shown
        ],
        "total_files": len(rows),
        "total_chars_saved": sum(r["saved"] for r in rows),
        "total_tokens_saved": sum(r["tokens_saved"] for r in rows),
        "skipped": [{"path": p, "reason": why} for p, why in skipped],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="simplicio_audit.py",
        description="Estimate token savings from compressing read-side files.",
    )
    parser.add_argument("paths", nargs="+", help="files or directories to scan")
    parser.add_argument("--top", type=int, default=None, help="limit rows shown")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--min-bytes", type=int, default=0, help="skip files smaller than N bytes"
    )
    args = parser.parse_args(argv)

    rows, skipped = audit(args.paths, min_bytes=args.min_bytes)

    if args.json:
        print(json.dumps(build_json(rows, skipped, top=args.top), ensure_ascii=False))
    else:
        print(render_table(rows, skipped, top=args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
