"""Integration test for `scripts/verify_adapters.py` — installer + filesystem contract, together.

Runs the real install pipeline (`install_lib.py`) into a throwaway target and checks that the
adapter's landed artifacts (skills, entry file with marker, hooks, settings.json wiring) actually
satisfy the contract — multiple components (installer, filesystem, marker format) exercised as a
whole, distinct from a pure-function unit test. We only exercise the "claude" runtime here (fast,
deterministic); the full 11-runtime sweep is `python3 scripts/verify_adapters.py` itself.
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIFY_ADAPTERS = os.path.join(REPO, "scripts", "verify_adapters.py")


def _run(args):
    return subprocess.run([sys.executable, VERIFY_ADAPTERS] + args, capture_output=True,
                          text=True, cwd=REPO, timeout=120)


def test_claude_adapter_install_contract_passes():
    r = _run(["claude"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS  claude" in r.stdout, r.stdout


def test_unknown_runtime_is_a_clean_error():
    r = _run(["not-a-real-runtime"])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "unknown runtime" in r.stdout.lower()


def test_tier1_shortcut_expands_to_claude_codex_cursor():
    r = _run(["tier1"])
    assert r.returncode == 0, r.stdout + r.stderr
    for rt in ("claude", "codex", "cursor"):
        assert ("PASS  %-12s" % rt) in r.stdout or ("FAIL  %-12s" % rt) in r.stdout, \
            "tier1 must attempt %s:\n%s" % (rt, r.stdout)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_verify_adapters_integration")
