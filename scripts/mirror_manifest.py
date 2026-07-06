#!/usr/bin/env python3
"""simplicio-loop — single source of truth for the "lean mirror" sets (#74).

`scripts/sync_plugin.py` (writes `plugin/`) and `scripts/claims_audit.py` (validates
`simplicio_loop/_bundle/` and `plugin/` parity) previously hard-coded the SAME lean subset of
hooks/scripts/tests independently. Editing one and forgetting the other let the syncer copy one
set while the auditor validated another — drift that passed the gate silently. This module is the
ONE place those sets are declared; both consumers import from here.

No behavior of its own — pure data, imported by `sync_plugin.py` and `claims_audit.py`.
"""

# The ONLY hook files the marketplace plugin ships: those wired in hooks.claude.json + their deps.
# loop_stop (Stop) · action_gate/orient_rewrite (PreToolUse) · orient_clamp (orient_rewrite shells
# out to it) · hooks.claude.json (the wiring) · pre-commit.py (auto-sync, #98).
LEAN_HOOKS = ["loop_stop.py", "action_gate.py", "orient_rewrite.py",
              "orient_clamp.py", "hooks.claude.json", "pre-commit.py"]

# Runtime helper scripts now transitively required by the shipped loop hook — mirrored into both
# plugin/scripts/ and simplicio_loop/_bundle/scripts/.
LEAN_SCRIPTS = ["hierarchical_planner.py", "cross_agent_wiki.py"]

# Minimal parity coverage for the shipped loop/runtime helpers — mirrored into both
# plugin/tests/ and simplicio_loop/_bundle/tests/.
LEAN_TESTS = ["_selfrun.py", "test_loop_e2e.py", "test_cross_agent_wiki.py"]


def selftest():
    checks = [
        ("LEAN_HOOKS non-empty", bool(LEAN_HOOKS)),
        ("LEAN_SCRIPTS non-empty", bool(LEAN_SCRIPTS)),
        ("LEAN_TESTS non-empty", bool(LEAN_TESTS)),
        ("LEAN_HOOKS has no dupes", len(LEAN_HOOKS) == len(set(LEAN_HOOKS))),
        ("LEAN_SCRIPTS has no dupes", len(LEAN_SCRIPTS) == len(set(LEAN_SCRIPTS))),
        ("LEAN_TESTS has no dupes", len(LEAN_TESTS) == len(set(LEAN_TESTS))),
    ]
    ok = all(v for _, v in checks)
    for name, v in checks:
        print("  [%s] %s" % ("ok" if v else "XX", name))
    print("mirror_manifest selftest: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(selftest())
    print(__doc__)
