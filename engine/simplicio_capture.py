"""Dry-run capture inspector for an LLM request payload.

Mirrors the headroom "capture/compare" idea: take an OpenAI/Anthropic-shaped
request body (`model`, `messages`, optional `system`) and show *exactly* what
Simplicio would compress and how many tokens it would save — WITHOUT sending
anything anywhere. This is a pure local analyzer: no network, ever.

For each message content (and the system prompt) it reports:
    before chars/tokens -> after chars/tokens -> saved, plus which algos fired
    (from `simplicio_compress.compress_report(...)["applied"]`).

It prints a per-message table and a TOTAL (tokens before -> after -> saved, %),
or structured JSON with `--json`.

Token counting prefers `simplicio_tokens.count_tokens` when that sibling module
is importable; otherwise it falls back to a `len(text) // 4` estimate. Char
counts and the compression itself always come from `simplicio_compress`.

Stdlib only (json, os, sys, argparse). Fail-open per message: a content block
that errors during compression is reported as a no-op (0 saved), never crashes
the run.

CLI:
    python3 simplicio_capture.py [--file payload.json | --stdin] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Make the engine dir importable so the sibling modules resolve regardless of
# the caller's cwd.
_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)

from simplicio_compress import compress_report  # noqa: E402  (after sys.path)

# Optional, more accurate token estimator. Fall back to chars/4 when absent.
try:
    from simplicio_tokens import count_tokens as _count_tokens  # noqa: E402

    _TOKENIZER = "simplicio_tokens"
except Exception:  # pragma: no cover - exercised only when the module is absent
    _count_tokens = None
    _TOKENIZER = "estimate(chars/4)"


def count_tokens(text: str) -> int:
    """Token count for `text`: real estimator if available, else chars // 4."""
    if not isinstance(text, str) or not text:
        return 0
    if _count_tokens is not None:
        try:
            return int(_count_tokens(text))
        except Exception:
            pass
    return len(text) // 4


# ---------------------------------------------------------------------------
# Payload flattening (OpenAI/Anthropic content shapes)
# ---------------------------------------------------------------------------
def _block_text(block: object) -> str:
    """Text of a single content block (str, {'text': ...}, or .text attr)."""
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        return str(block.get("text", ""))
    text_attr = getattr(block, "text", None)
    return text_attr if isinstance(text_attr, str) else ""


def _content_text(content: object) -> str:
    """Flatten a message `content` (str | list-of-blocks | other) to a string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_block_text(b) for b in content)
    return str(content)


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------
def analyze_text(label: str, text: str) -> dict:
    """Capture one content string. Fail-open: errors => a 0-saved no-op row."""
    before_chars = len(text)
    before_tokens = count_tokens(text)
    try:
        rep = compress_report(text)
        # compress_report doesn't return the compressed text; recompute it via
        # the same module so token counting reflects the real output.
        from simplicio_compress import compress as _compress

        compressed = _compress(text)
        applied = list(rep.get("applied", []))
        after_chars = len(compressed)
        after_tokens = count_tokens(compressed)
    except Exception as exc:  # fail-open
        compressed = text
        applied = []
        after_chars = before_chars
        after_tokens = before_tokens
        return {
            "label": label,
            "before_chars": before_chars,
            "after_chars": after_chars,
            "saved_chars": 0,
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "saved_tokens": 0,
            "pct_tokens": 0.0,
            "applied": applied,
            "error": str(exc),
        }

    saved_chars = before_chars - after_chars
    saved_tokens = before_tokens - after_tokens
    pct_tokens = (
        round((saved_tokens / before_tokens) * 100, 2) if before_tokens else 0.0
    )
    return {
        "label": label,
        "before_chars": before_chars,
        "after_chars": after_chars,
        "saved_chars": saved_chars,
        "before_tokens": before_tokens,
        "after_tokens": after_tokens,
        "saved_tokens": saved_tokens,
        "pct_tokens": pct_tokens,
        "applied": applied,
    }


def analyze_payload(obj: dict) -> dict:
    """Analyze a parsed request body. Returns {model, tokenizer, rows, total}."""
    model = obj.get("model", "") if isinstance(obj, dict) else ""
    rows: list[dict] = []

    system = obj.get("system") if isinstance(obj, dict) else None
    if system:
        rows.append(analyze_text("system", _content_text(system)))

    messages = obj.get("messages", []) if isinstance(obj, dict) else []
    if not isinstance(messages, list):
        messages = []
    for i, msg in enumerate(messages):
        if isinstance(msg, dict):
            role = msg.get("role", "?")
            content = msg.get("content")
        else:
            role = "?"
            content = msg
        label = "msg[%d] %s" % (i, role)
        rows.append(analyze_text(label, _content_text(content)))

    total = {
        "before_chars": sum(r["before_chars"] for r in rows),
        "after_chars": sum(r["after_chars"] for r in rows),
        "saved_chars": sum(r["saved_chars"] for r in rows),
        "before_tokens": sum(r["before_tokens"] for r in rows),
        "after_tokens": sum(r["after_tokens"] for r in rows),
        "saved_tokens": sum(r["saved_tokens"] for r in rows),
    }
    bt = total["before_tokens"]
    total["pct_tokens"] = (
        round((total["saved_tokens"] / bt) * 100, 2) if bt else 0.0
    )
    return {
        "model": model,
        "tokenizer": _TOKENIZER,
        "messages": len(rows),
        "rows": rows,
        "total": total,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_text(result: dict) -> str:
    lines: list[str] = []
    lines.append("simplicio-cli capture (dry-run — nothing was sent)")
    lines.append("model: %s   tokenizer: %s" % (
        result.get("model") or "(unset)", result.get("tokenizer", "")))
    lines.append("")

    header = "%-22s %12s %12s %10s  %s" % (
        "message", "tok before", "tok after", "saved", "algos")
    lines.append(header)
    lines.append("-" * len(header))

    for r in result["rows"]:
        algos = ", ".join(r["applied"]) if r["applied"] else "-"
        note = ""
        if r.get("error"):
            note = "  [compress error: %s]" % r["error"]
        lines.append("%-22s %12d %12d %10d  %s%s" % (
            r["label"][:22],
            r["before_tokens"],
            r["after_tokens"],
            r["saved_tokens"],
            algos,
            note,
        ))

    t = result["total"]
    lines.append("-" * len(header))
    lines.append("%-22s %12d %12d %10d  (%.2f%%)" % (
        "TOTAL",
        t["before_tokens"],
        t["after_tokens"],
        t["saved_tokens"],
        t["pct_tokens"],
    ))
    lines.append("")
    lines.append("chars: %d -> %d (saved %d)" % (
        t["before_chars"], t["after_chars"], t["saved_chars"]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _read_source(args) -> str:
    if args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            return fh.read()
    # --stdin, or default to stdin when no --file given.
    return sys.stdin.read()


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="simplicio_capture",
        description=(
            "Dry-run inspector: show what Simplicio would compress in an LLM "
            "request payload and how many tokens it would save. Never sends "
            "anything."
        ),
    )
    src = parser.add_mutually_exclusive_group()
    src.add_argument(
        "--file",
        metavar="PATH",
        help="read the JSON request body from PATH.",
    )
    src.add_argument(
        "--stdin",
        action="store_true",
        help="read the JSON request body from stdin (default when no --file).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit structured JSON instead of the text table.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    try:
        raw = _read_source(args)
    except OSError as exc:
        sys.stderr.write("error: cannot read input: %s\n" % exc)
        return 2

    try:
        obj = json.loads(raw)
    except (ValueError, TypeError) as exc:
        sys.stderr.write(
            "error: request body is not valid JSON: %s\n" % exc)
        return 2

    if not isinstance(obj, dict):
        sys.stderr.write(
            "error: request body must be a JSON object "
            "(with 'messages'/'system'), got %s\n" % type(obj).__name__)
        return 2

    result = analyze_payload(obj)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(render_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
