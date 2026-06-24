#!/usr/bin/env python3
"""simplicio-loop — local check runner (the "CI" you run yourself, no paid minutes).

Runs the whole verification gate locally — deterministic, stdlib-only, cross-platform:

  1. claims-audit   `scripts/claims_audit.py` (referenced scripts exist · extension-point count
                    consistent · cited commands run · _bundle ≡ source)
  2. test suite     pytest if installed (`pytest -q tests/`); otherwise each `tests/test_*.py`
                    self-runs on bare python3 (the suite needs no pip).

Exit 0 only when everything passes — so it gates a commit/push. Wire it as a git pre-push hook to
keep `main` honest with zero CI cost:

    printf '#!/bin/sh\\npython3 scripts/check.py\\n' > .git/hooks/pre-push
    chmod +x .git/hooks/pre-push

Usage:
    python3 scripts/check.py              # audit + tests
    python3 scripts/check.py --audit-only
    python3 scripts/check.py --tests-only
"""
import os
import subprocess
import sys
import glob

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def _hr(title):
    print("\n=== %s ===" % title)


def run_audit():
    _hr("claims-audit")
    r = subprocess.run([sys.executable, os.path.join(HERE, "claims_audit.py")], cwd=REPO)
    return r.returncode == 0


def _have_pytest():
    return subprocess.run([sys.executable, "-c", "import pytest"],
                          capture_output=True).returncode == 0


def run_tests():
    tests_dir = os.path.join(REPO, "tests")
    if not os.path.isdir(tests_dir):
        print("no tests/ dir — skipping")
        return True
    if _have_pytest():
        _hr("pytest tests/")
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/"], cwd=REPO)
        return r.returncode == 0
    # zero-dependency fallback: each test file self-runs on bare python3
    _hr("tests/ (stdlib self-run — pytest not installed)")
    ok = True
    for tf in sorted(glob.glob(os.path.join(tests_dir, "test_*.py"))):
        r = subprocess.run([sys.executable, tf], cwd=REPO)
        ok = ok and r.returncode == 0
    return ok


def main():
    args = sys.argv[1:]
    audit_ok = tests_ok = True
    if "--tests-only" not in args:
        audit_ok = run_audit()
    if "--audit-only" not in args:
        tests_ok = run_tests()
    ok = audit_ok and tests_ok
    print("\ncheck: %s  (audit=%s · tests=%s)" % (
        "PASS" if ok else "FAIL", "ok" if audit_ok else "FAIL", "ok" if tests_ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
