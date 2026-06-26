"""Compression-quality eval + regression harness for simplicio-loop.

A built-in labeled corpus spanning the
content types the compression pipeline is meant to handle, run end-to-end
(``compress`` then ``compress_extra``), with a set of hard INVARIANTS that act
as a CI regression gate. If a future change corrupts prose/code or stops saving
on logs/hex/progress noise, this harness fails NON-ZERO.

Stdlib only. No network. Imports the sibling ``simplicio_compress`` (required)
and ``simplicio_compress_extra`` (optional — degrades gracefully if absent).

Usage:
    python3 simplicio_evals.py            # human table + invariant report
    python3 simplicio_evals.py --json     # machine-readable results + gate

Exit code: 0 if every invariant passes, 1 otherwise.
"""

from __future__ import annotations

import json
import os
import sys

# --- sibling imports (add engine dir to sys.path; fall back gracefully) ------
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from simplicio_compress import compress, compress_report  # type: ignore
except Exception as exc:  # pragma: no cover - hard dependency
    sys.stderr.write(
        "FATAL: cannot import simplicio_compress (required): %s\n" % exc
    )
    raise SystemExit(2)

try:
    from simplicio_compress_extra import (  # type: ignore
        EXTRA_ALGOS as _EXTRA_ALGOS,
        compress_extra,
    )

    _HAVE_EXTRA = True
except Exception:  # extra module absent — degrade to base pipeline only
    _EXTRA_ALGOS = []  # type: ignore[assignment]
    _HAVE_EXTRA = False

    def compress_extra(text: str) -> str:  # type: ignore[misc]
        return text


# --- built-in labeled corpus -------------------------------------------------
# ~10 samples, one per content type the pipeline targets. Each value is the raw
# "before" text. Verbose/log/hex/progress samples are sized so the relevant
# algos fire; prose/code samples must come back byte-identical.

_ESC = "\x1b"  # ANSI escape introducer, kept out of the source as a literal.

_LOG_LINES = (
    "%s[32m2024-01-01 10:00:00 INFO  starting worker pool%s[0m\n" % (_ESC, _ESC)
    + ("%s[31m2024-01-01 10:00:01 ERROR connection refused%s[0m\n" % (_ESC, _ESC)) * 12
    + "=" * 60 + "\n"
    + "%s[33m2024-01-01 10:00:02 WARN  retrying in 5s%s[0m\n" % (_ESC, _ESC)
    + ("2024-01-01 10:00:03 DEBUG heartbeat ok\n") * 8
    + "-" * 60 + "\n"
    + "%s[32m2024-01-01 10:00:09 INFO  shutdown complete%s[0m\n" % (_ESC, _ESC)
)

_HEX_LINES = "\n".join(
    " ".join("%02x" % ((i * 16 + j) & 0xFF) for j in range(40))
    for i in range(12)
) + "\n"

_PROGRESS_LINES = "".join(
    "Processing item %d/2000 ... done in %dms\n" % (i, 12 + (i % 7))
    for i in range(1, 41)
)

CORPUS: "dict[str, str]" = {
    "english_prose": (
        "The quick brown fox jumps over the lazy dog. This sentence is a "
        "perfectly ordinary piece of English prose, with normal spacing and "
        "punctuation. It should never be altered by a compression pass that "
        "claims to preserve meaning. Words matter, and so do the spaces "
        "between them, at least to a human reader who expects to see exactly "
        "what was written without any silent edits.\n"
        "A second paragraph follows, separated by a single newline, just to "
        "give the pipeline something multi-line to chew on without offering "
        "any machine-shaped runs it could legitimately fold away.\n"
    ),
    "python_code": (
        "import os\n"
        "import sys\n"
        "\n"
        "\n"
        "def add(a, b):\n"
        "    # leading spaces inside this line are load-bearing\n"
        "    result = a + b\n"
        "    return result\n"
        "\n"
        "\n"
        "class Greeter:\n"
        "    def __init__(self, name):\n"
        "        self.name = name\n"
        "\n"
        "    def hello(self):\n"
        "        return f'hello {self.name}'\n"
    ),
    "js_minified": (
        "var app=function(){"
        + "var x=0;for(var i=0;i<1000;i++){x+=Math.sqrt(i)*Math.sin(i)/Math.cos(i+1);}"
        * 6
        + "return x;};app();\n"
    ),
    "verbose_logs": _LOG_LINES,
    "json_pretty": json.dumps(
        {
            "name": "simplicio",
            "version": "1.0.0",
            "nested": {"a": [1, 2, 3], "b": {"c": True, "d": None}},
            "items": [{"id": i, "label": "row-%d" % i} for i in range(20)],
        },
        indent=4,
    )
    + "\n",
    "hex_dump": _HEX_LINES,
    "markdown_table": (
        "| Name        | Role          | Status      |\n"
        "| ----------- | ------------- | ----------- |\n"
        "| alice       | orchestrator  | active      |\n"
        "| bob         | reviewer      | active      |\n"
        "| carol       | compressor    | idle        |\n"
        "| dave        | learner       | active      |\n"
    ),
    "stack_trace": (
        "Traceback (most recent call last):\n"
        '  File "main.py", line 42, in <module>\n'
        "    run()\n"
        '  File "main.py", line 30, in run\n'
        "    step()\n"
        '  File "main.py", line 18, in step\n'
        "    raise ValueError('boom')\n"
        "ValueError: boom\n"
    ),
    "numbered_progress": _PROGRESS_LINES,
    "mixed_doc": (
        "# Release notes\n"
        "\n"
        "Some prose describing the release in plain English.\n"
        "\n"
        "```\n"
        + ("2024-06-01 09:00:00 INFO  built artifact\n") * 9
        + "```\n"
        "\n"
        "| key | value |\n"
        "| --- | ----- |\n"
        "| a   | 1     |\n"
        "| b   | 2     |\n"
        "\n"
        "Closing paragraph, also ordinary prose.\n"
    ),
}


# --- pipeline ----------------------------------------------------------------
def run_pipeline(text: str) -> "dict":
    """Run the full pipeline (compress then compress_extra) and report.

    Returns: before, after, saved, pct, applied (list of algo names, base ones
    from compress_report plus any extra algos that strictly shrank the text).
    """
    base = compress_report(text)
    after_base = compress(text)  # == base["after"] length, same string
    applied = list(base["applied"])

    final = after_base
    if _HAVE_EXTRA:
        # Detect which extra algos fire by applying them one at a time, in the
        # same order compress_extra uses, on the running text.
        cur = after_base
        for name, fn in _EXTRA_ALGOS:
            try:
                out = fn(cur)
            except Exception:
                continue
            if isinstance(out, str) and len(out) < len(cur):
                cur = out
                applied.append(name)
        final = compress_extra(after_base)
        # `cur` and `final` are byte-identical by construction; use `final`.

    before = len(text)
    after = len(final)
    saved = before - after
    pct = round((saved / before) * 100, 2) if before else 0.0
    return {
        "before": before,
        "after": after,
        "saved": saved,
        "pct": pct,
        "applied": applied,
        "output": final,
    }


def evaluate() -> "dict":
    """Run every corpus sample through the pipeline; return results map."""
    results = {}
    for name, text in CORPUS.items():
        results[name] = run_pipeline(text)
    return results


# --- invariants (the regression gate) ---------------------------------------
def check_invariants(results: "dict") -> "list[dict]":
    """Evaluate the 4 hard invariants. Each entry: name, passed, detail."""
    checks: "list[dict]" = []

    # 1. log/hex/progress noise must save > 40%.
    save_targets = ("verbose_logs", "hex_dump", "numbered_progress")
    failures = []
    for s in save_targets:
        pct = results[s]["pct"]
        if pct <= 40.0:
            failures.append("%s=%.2f%%" % (s, pct))
    checks.append(
        {
            "name": "noise_samples_save_over_40pct",
            "passed": not failures,
            "detail": "all > 40%"
            if not failures
            else "under-threshold: " + ", ".join(failures),
        }
    )

    # 2. prose and code returned byte-identical (no corruption).
    identical_targets = ("english_prose", "python_code")
    corrupted = []
    for s in identical_targets:
        if results[s]["output"] != CORPUS[s]:
            corrupted.append(s)
    checks.append(
        {
            "name": "prose_and_code_byte_identical",
            "passed": not corrupted,
            "detail": "byte-identical"
            if not corrupted
            else "CORRUPTED: " + ", ".join(corrupted),
        }
    )

    # 3. compress is idempotent for every sample.
    non_idempotent = []
    for name, text in CORPUS.items():
        once = compress(text)
        twice = compress(once)
        if twice != once:
            non_idempotent.append(name)
    checks.append(
        {
            "name": "compress_idempotent",
            "passed": not non_idempotent,
            "detail": "idempotent for all samples"
            if not non_idempotent
            else "NOT idempotent: " + ", ".join(non_idempotent),
        }
    )

    # 4. output never larger than input for any sample (full pipeline).
    grew = []
    for name, res in results.items():
        if res["after"] > res["before"]:
            grew.append("%s(+%d)" % (name, res["after"] - res["before"]))
    checks.append(
        {
            "name": "output_never_larger_than_input",
            "passed": not grew,
            "detail": "never grew"
            if not grew
            else "GREW: " + ", ".join(grew),
        }
    )

    return checks


# --- rendering ---------------------------------------------------------------
def _avg_pct(results: "dict") -> float:
    if not results:
        return 0.0
    return round(sum(r["pct"] for r in results.values()) / len(results), 2)


def render_table(results: "dict") -> str:
    rows = []
    name_w = max(len("sample"), max(len(k) for k in results))
    header = "%-*s  %8s  %s" % (name_w, "sample", "%saved", "algos applied")
    rows.append(header)
    rows.append("-" * len(header))
    for name in CORPUS:  # stable, labeled order
        res = results[name]
        algos = ", ".join(res["applied"]) if res["applied"] else "(none)"
        rows.append(
            "%-*s  %7.2f%%  %s" % (name_w, name, res["pct"], algos)
        )
    rows.append("-" * len(header))
    rows.append(
        "%-*s  %7.2f%%  (aggregate avg)" % (name_w, "AVG", _avg_pct(results))
    )
    return "\n".join(rows)


def render_invariants(checks: "list[dict]") -> str:
    rows = ["", "Invariants (regression gate):"]
    for c in checks:
        mark = "PASS" if c["passed"] else "FAIL"
        rows.append("  [%s] %s — %s" % (mark, c["name"], c["detail"]))
    return "\n".join(rows)


def main(argv: "list[str]") -> int:
    as_json = "--json" in argv[1:]
    results = evaluate()
    checks = check_invariants(results)
    all_pass = all(c["passed"] for c in checks)

    if as_json:
        payload = {
            "extra_module_present": _HAVE_EXTRA,
            "aggregate_avg_pct": _avg_pct(results),
            "samples": {
                name: {
                    "before": r["before"],
                    "after": r["after"],
                    "saved": r["saved"],
                    "pct": r["pct"],
                    "applied": r["applied"],
                }
                for name, r in results.items()
            },
            "invariants": checks,
            "all_invariants_passed": all_pass,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(render_table(results))
        print(render_invariants(checks))
        print(
            "\nGATE: %s (%d/%d invariants passed)"
            % (
                "PASS" if all_pass else "FAIL",
                sum(1 for c in checks if c["passed"]),
                len(checks),
            )
        )
        if not _HAVE_EXTRA:
            print("note: simplicio_compress_extra absent — base pipeline only.")

    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
