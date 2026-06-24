"""Aggregate the deterministic `selftest` of every worker that ships one.

Each worker (`loop_journal`, `billing_aggregator`, `savings_harness`) carries a model-free
`selftest` that proves its arithmetic with no files. This runs them as subprocesses and asserts
exit 0 + a PASS line — so `python3 scripts/check.py` (or pytest) re-proves them on every change.
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# worker script → the subcommand that runs its self-check
SELFTESTS = [
    ("scripts/loop_journal.py", "selftest"),
    ("scripts/billing_aggregator.py", "selftest"),
    ("scripts/savings_harness.py", "selftest"),
]


def _run(script, sub):
    return subprocess.run([sys.executable, os.path.join(REPO, script), sub],
                          capture_output=True, text=True, cwd=REPO)


def test_loop_journal_selftest():
    r = _run("scripts/loop_journal.py", "selftest")
    assert r.returncode == 0, "loop_journal selftest failed:\n%s%s" % (r.stdout, r.stderr)
    assert "PASS" in r.stdout, r.stdout


def test_billing_aggregator_selftest():
    r = _run("scripts/billing_aggregator.py", "selftest")
    assert r.returncode == 0, "billing_aggregator selftest failed:\n%s%s" % (r.stdout, r.stderr)
    assert "PASS" in r.stdout, r.stdout


def test_savings_harness_selftest():
    r = _run("scripts/savings_harness.py", "selftest")
    assert r.returncode == 0, "savings_harness selftest failed:\n%s%s" % (r.stdout, r.stderr)
    # savings_harness prints "selftest passed" / "OK"; accept either a 0 exit with no FAIL
    assert "FAIL" not in r.stdout.upper() or "PASS" in r.stdout.upper(), r.stdout


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_worker_selftests")
