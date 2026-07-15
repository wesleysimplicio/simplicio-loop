#!/usr/bin/env python3
"""simplicio-loop — CI Quality Gate: regression-test-required gate (#277, extended by #283).

Issue #277 acceptance criterion: "Todo bug recebe teste de regressao" (every bug fix ships with a
regression test). Enforces a narrow, mechanical version of that: any pull request that touches a
source file under `simplicio_loop/`, `engine/`, `scripts/`, or `hooks/` must ALSO touch at least
one file under `tests/` in the same diff. It cannot verify a test is *meaningful*, only that a
change didn't land with zero test-side footprint -- a human reviewer still owns whether the added
test actually covers the regression.

Exemptions (source-only changes that legitimately have no test-side counterpart):
  - docs/comments-only changes (diff has no changed non-comment/non-blank lines) -- best-effort,
    see `_is_trivial_diff`.
  - files matching `--exempt-glob` (repeatable), e.g. `--exempt-glob "scripts/install.*"`.

Usage:
    python3 scripts/regression_test_gate.py --base origin/main
    python3 scripts/regression_test_gate.py --base origin/main --exempt-glob "*.md"
    python3 scripts/regression_test_gate.py --base origin/main --emit-json report.json

#283: `--emit-json PATH` writes the raw verdict dict (see `evaluate_regression_gate`) to PATH so
`scripts/quality_matrix.py populate` (and any independent re-verifier) can consume the exact same
structured evidence this gate itself computed, instead of re-deriving it from stdout text.

Exit codes: 0 = gate satisfied (or nothing to check), 1 = source changed without a test change.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
import time

SOURCE_PREFIXES = ("simplicio_loop/", "engine/", "scripts/", "hooks/")
TEST_PREFIX = "tests/"

DEFAULT_EXEMPT_GLOBS = [
    "*.md",
    "scripts/install.*",
    "scripts/setup_simplicio.sh",
    "scripts/update.sh",
]


def _changed_files(base: str) -> list:
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
    )
    if out.returncode != 0:
        # Fallback: diff against merge-base failed (e.g. shallow clone) -- try a plain diff.
        out = subprocess.run(
            ["git", "diff", "--name-only", base, "HEAD"],
            capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
        )
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


def _is_trivial_diff(base: str, path: str) -> bool:
    """True if every changed line in `path` is blank or looks like a comment/docstring line."""
    out = subprocess.run(
        ["git", "diff", f"{base}...HEAD", "--", path],
        capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
    )
    changed_lines = [
        ln[1:] for ln in out.stdout.splitlines()
        if (ln.startswith("+") or ln.startswith("-")) and not ln.startswith(("+++", "---"))
    ]
    if not changed_lines:
        return True
    for ln in changed_lines:
        stripped = ln.strip()
        if not stripped:
            continue
        if stripped.startswith(("#", "//", '"""', "'''", "*")):
            continue
        return False
    return True


def evaluate_regression_gate(base: str = "origin/main", exempt_globs=None) -> dict:
    """Compute the regression-test-required verdict as a plain data dict.

    #283: this is the reusable core `main()` wraps -- extracted so
    `scripts/quality_matrix.py populate` (and any independent re-verifier) can call it directly
    or re-invoke this script as a subprocess and load the exact same structured result via
    `--emit-json`, rather than re-deriving pass/fail from prose stdout.
    """
    exempt = DEFAULT_EXEMPT_GLOBS + list(exempt_globs or [])
    changed = _changed_files(base)
    touched_tests = any(f.startswith(TEST_PREFIX) for f in changed)
    source_changes = [
        f for f in changed
        if f.startswith(SOURCE_PREFIXES)
        and not any(fnmatch.fnmatch(f, g) for g in exempt)
        and not _is_trivial_diff(base, f)
    ]
    if not changed:
        ok = True
        detail = "no changes vs base; nothing to check"
    elif not source_changes:
        ok = True
        detail = "no non-trivial source changes requiring a test"
    elif touched_tests:
        ok = True
        detail = f"{len(source_changes)} source file(s) changed, tests/ also touched"
    else:
        ok = False
        detail = "source changed without any accompanying tests/ change: " + ", ".join(source_changes)
    return {
        "schema": "simplicio.regression-gate/v1",
        "ok": ok,
        "base": base,
        "changed_files": changed,
        "source_changes": source_changes,
        "touched_tests": touched_tests,
        "detail": detail,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="origin/main", help="base ref to diff against (default: origin/main)")
    parser.add_argument("--exempt-glob", action="append", default=[], help="glob(s) exempt from the gate (repeatable)")
    parser.add_argument("--emit-json", default=None, help="write the raw verdict dict to this path (#283)")
    args = parser.parse_args()

    verdict = evaluate_regression_gate(args.base, args.exempt_glob)

    if args.emit_json:
        with open(args.emit_json, "w", encoding="utf-8") as fh:
            json.dump(verdict, fh, indent=2, sort_keys=True)
            fh.write("\n")

    if not verdict["changed_files"]:
        print("[regression-test-gate] no changes vs base; nothing to check.")
        return 0
    if not verdict["source_changes"]:
        print("[regression-test-gate] OK: no non-trivial source changes requiring a test.")
        return 0
    if verdict["touched_tests"]:
        print(f"[regression-test-gate] OK: {len(verdict['source_changes'])} source file(s) changed, tests/ also touched.")
        return 0

    print("[regression-test-gate] FAILED: source changed without any accompanying tests/ change:", file=sys.stderr)
    for f in verdict["source_changes"]:
        print(f"  - {f}", file=sys.stderr)
    print(
        "\nAdd or update a test under tests/ covering this change, or exempt the file with "
        "--exempt-glob if it genuinely has no test-side counterpart.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
