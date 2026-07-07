#!/usr/bin/env python3
"""simplicio-loop — local check runner (the "CI" you run yourself, no paid minutes).

Runs the whole verification gate locally — deterministic, stdlib-only, cross-platform:

  1. claims-audit   `scripts/claims_audit.py` (referenced scripts exist · extension-point count
                    consistent · cited commands run, including flow/impact audit selftests
                    · _bundle ≡ source)
  2. test suite     pytest if installed (`pytest -q tests/`); otherwise each `tests/test_*.py`
                    self-runs on bare python3 (the suite needs no pip).
  3. loop-contract  `scripts/check_loop_contract.py` — validates the exported
                    `simplicio.loop-execution/v1` converge/drain fixtures
                    (`contracts/loop-execution/v1/`) against the real `hooks/loop_stop.py` /
                    `scripts/loop_journal.py` producers (#115).

Exit 0 only when everything passes — so it gates a commit/push. Wire it as a git pre-push hook to
keep `main` honest with zero CI cost:

    printf '#!/bin/sh\\npython3 scripts/check.py\\n' > .git/hooks/pre-push
    chmod +x .git/hooks/pre-push

Usage:
    python3 scripts/check.py              # audit + tests + loop-contract
    python3 scripts/check.py --audit-only
    python3 scripts/check.py --tests-only
    python3 scripts/check.py --loop-contract-only
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


def run_loop_contract():
    _hr("loop-contract (simplicio.loop-execution/v1)")
    path = os.path.join(HERE, "check_loop_contract.py")
    if not os.path.exists(path):
        print("scripts/check_loop_contract.py not found — skipping")
        return True
    r = subprocess.run([sys.executable, path], cwd=REPO)
    return r.returncode == 0


def main():
    args = sys.argv[1:]
    only_flags = {"--audit-only", "--tests-only", "--loop-contract-only"}
    any_only = any(a in args for a in only_flags)
    audit_ok = tests_ok = contract_ok = True
    if not any_only or "--audit-only" in args:
        audit_ok = run_audit()
    if not any_only or "--tests-only" in args:
        tests_ok = run_tests()
    if not any_only or "--loop-contract-only" in args:
        contract_ok = run_loop_contract()
    ok = audit_ok and tests_ok and contract_ok
    print("\ncheck: %s  (audit=%s · tests=%s · loop-contract=%s)" % (
        "PASS" if ok else "FAIL", "ok" if audit_ok else "FAIL", "ok" if tests_ok else "FAIL",
        "ok" if contract_ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
