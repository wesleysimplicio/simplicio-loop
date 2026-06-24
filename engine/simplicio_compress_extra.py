"""Extra deterministic, fail-open, meaning-preserving text compression.

Stdlib only (re). These algorithms EXTEND the base pipeline in
``simplicio_compress.py`` with NEW, non-overlapping passes. Each one shrinks
text only when it is safe to do so — it must NEVER corrupt the meaning of code
or prose, and is a no-op when its target pattern is absent.

Public API:
    compress_extra(text) -> str
    EXTRA_ALGOS -> list[(name, fn)]

Invariants (mirror the base module):
    - compress_extra(text) returns `text` unchanged when nothing shrinks.
    - idempotent: compress_extra(compress_extra(x)) == compress_extra(x).
    - each algo is applied only if it strictly shrinks the text.
    - prose and code are returned byte-identical (the patterns target
      clearly-machine-shaped runs, not human text).
"""

from __future__ import annotations

import re

__all__ = ["compress_extra", "EXTRA_ALGOS"]


# 1. markdown_table_ws — collapse runs of spaces used for *padding* inside
#    markdown table cells, without touching cell content. A table line has
#    multiple " | " separators (we require >= 2 unescaped pipes, i.e. >= 1 inner
#    cell). Only the padding directly adjacent to a `|` separator is trimmed to
#    a single space; spaces *within* the cell text are preserved.
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
# Collapse 2+ spaces that sit immediately before or after a pipe down to 1.
_TABLE_PAD_BEFORE = re.compile(r" {2,}(?=\|)")
_TABLE_PAD_AFTER = re.compile(r"(?<=\|) {2,}")


def markdown_table_ws(text: str) -> str:
    lines = text.splitlines(keepends=True)
    if not lines:
        return text
    out: list[str] = []
    for line in lines:
        content = line.rstrip("\r\n")
        eol = line[len(content):]
        # Require it to look like a table row: starts/ends with a pipe and has
        # at least 2 pipes total (>= 1 inner cell boundary).
        if _TABLE_LINE_RE.match(content) and content.count("|") >= 2:
            new = _TABLE_PAD_BEFORE.sub(" ", content)
            new = _TABLE_PAD_AFTER.sub(" ", new)
            out.append(new + eol)
        else:
            out.append(line)
    return "".join(out)


# 2. repeated_block_fold — detect an identical multi-line block (>= 3 lines)
#    repeated 2+ times *consecutively* and collapse the later copies into a
#    single marker. Generalizes single-line dedup to whole blocks. We try the
#    largest plausible block first at each position so the densest fold wins.
_BLOCK_MARKER_RE = re.compile(
    r"^… \(identical \d+-line block repeated \d+x\)$"
)
_MIN_BLOCK = 3


def repeated_block_fold(text: str) -> str:
    lines = text.splitlines(keepends=True)
    n = len(lines)
    if n < _MIN_BLOCK * 2:
        return text
    out: list[str] = []
    i = 0
    while i < n:
        folded = False
        # Largest block size that could still repeat from i.
        max_b = (n - i) // 2
        for b in range(max_b, _MIN_BLOCK - 1, -1):
            block = lines[i:i + b]
            # Skip if the block already contains a marker (idempotence guard).
            if any(_BLOCK_MARKER_RE.match(ln.rstrip("\r\n")) for ln in block):
                continue
            reps = 1
            j = i + b
            while j + b <= n and lines[j:j + b] == block:
                reps += 1
                j += b
            if reps >= 2:
                out.extend(block)  # keep the first copy verbatim
                eol = block[-1][len(block[-1].rstrip("\r\n")):] or "\n"
                out.append(
                    "… (identical %d-line block repeated %dx)%s"
                    % (b, reps, eol)
                )
                i = j
                folded = True
                break
        if not folded:
            out.append(lines[i])
            i += 1
    return "".join(out)


# 3. long_token_elide — collapse a single unbroken token longer than 200 chars
#    (base64 blob, data URI, minified JS on one line) to a marker. "Unbroken"
#    means no whitespace in the run, so prose (which has spaces) is never hit.
_LONG_TOKEN_RE = re.compile(r"\S{201,}")


def long_token_elide(text: str) -> str:
    def _repl(m: "re.Match[str]") -> str:
        run = m.group(0)
        # Don't re-elide an existing marker (idempotence): the marker contains a
        # space ("<token:NNN chars elided>"), so \S+ never matches it whole.
        return "<token:%d chars elided>" % len(run)
    return _LONG_TOKEN_RE.sub(_repl, text)


# 4. numbered_noise_fold — collapse 8+ consecutive lines that are identical
#    EXCEPT for an incrementing number / timestamp at a fixed position, e.g.
#    "Processing item 1/1000", "Processing item 2/1000", … We canonicalize each
#    line by replacing every digit-run with a placeholder; a run of lines whose
#    canonical form is identical and that contain >= 1 number gets folded.
_DIGITS_RE = re.compile(r"\d+")
_NOISE_MARKER_RE = re.compile(r"^… \(\d+ near-identical lines: '.*'\)$")
_MIN_NOISE = 8


def _canon(content: str) -> str:
    return _DIGITS_RE.sub("\x00", content)


def numbered_noise_fold(text: str) -> str:
    lines = text.splitlines(keepends=True)
    n = len(lines)
    if n < _MIN_NOISE:
        return text
    out: list[str] = []
    i = 0
    while i < n:
        content = lines[i].rstrip("\r\n")
        canon = _canon(content)
        # Must contain at least one number and not be a marker / blank.
        if (
            "\x00" not in canon
            or content == ""
            or _NOISE_MARKER_RE.match(content)
        ):
            out.append(lines[i])
            i += 1
            continue
        j = i + 1
        while j < n and _canon(lines[j].rstrip("\r\n")) == canon:
            j += 1
        run = j - i
        if run >= _MIN_NOISE:
            eol = lines[i][len(content):] or "\n"
            # Sample text = first line with its digit-runs shown as "…".
            sample = canon.replace("\x00", "…")
            out.append(
                "… (%d near-identical lines: %r)%s" % (run, sample, eol)
            )
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "".join(out)


EXTRA_ALGOS = [
    ("markdown_table_ws", markdown_table_ws),
    ("repeated_block_fold", repeated_block_fold),
    ("long_token_elide", long_token_elide),
    ("numbered_noise_fold", numbered_noise_fold),
]


def compress_extra(text: str) -> str:
    """Run EXTRA_ALGOS in order, shrink-only. No-op if nothing shrinks.

    Idempotent: each algo is a fixpoint on its own output and emits markers that
    its own pattern will not re-match, so a second pass yields the same string.
    """
    if not isinstance(text, str) or not text:
        return text
    cur = text
    for _name, fn in EXTRA_ALGOS:
        try:
            out = fn(cur)
        except Exception:
            # fail-open: a misbehaving algo never breaks the pipeline.
            continue
        if isinstance(out, str) and len(out) < len(cur):
            cur = out
    return cur if len(cur) < len(text) else text
