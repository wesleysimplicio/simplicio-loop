#!/usr/bin/env python3
"""simplicio-loop — CI Quality Gate: regression-test-required gate (#277).

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

Exit codes: 0 = gate satisfied (or nothing to check), 1 = source changed without a test change.
"""
from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys

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
        capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        # Fallback: diff against merge-base failed (e.g. shallow clone) -- try a plain diff.
        out = subprocess.run(
            ["git", "diff", "--name-only", base, "HEAD"],
            capture_output=True, text=True, check=False,
        )
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


def _is_trivial_diff(base: str, path: str) -> bool:
    """True if every changed line in `path` is blank or looks like a comment/docstring line."""
    out = subprocess.run(
        ["git", "diff", f"{base}...HEAD", "--", path],
        capture_output=True, text=True, check=False,
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="origin/main", help="base ref to diff against (default: origin/main)")
    parser.add_argument("--exempt-glob", action="append", default=[], help="glob(s) exempt from the gate (repeatable)")
    args = parser.parse_args()

    exempt_globs = DEFAULT_EXEMPT_GLOBS + args.exempt_glob
    changed = _changed_files(args.base)

    if not changed:
        print("[regression-test-gate] no changes vs base; nothing to check.")
        return 0

    touched_tests = any(f.startswith(TEST_PREFIX) for f in changed)
    source_changes = [
        f for f in changed
        if f.startswith(SOURCE_PREFIXES)
        and not any(fnmatch.fnmatch(f, g) for g in exempt_globs)
        and not _is_trivial_diff(args.base, f)
    ]

    if not source_changes:
        print("[regression-test-gate] OK: no non-trivial source changes requiring a test.")
        return 0

    if touched_tests:
        print(f"[regression-test-gate] OK: {len(source_changes)} source file(s) changed, tests/ also touched.")
        return 0

    print("[regression-test-gate] FAILED: source changed without any accompanying tests/ change:", file=sys.stderr)
    for f in source_changes:
        print(f"  - {f}", file=sys.stderr)
    print(
        "\nAdd or update a test under tests/ covering this change, or exempt the file with "
        "--exempt-glob if it genuinely has no test-side counterpart.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
