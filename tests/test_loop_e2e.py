"""End-to-end test of the loop driver (`hooks/loop_stop.py`) — the contract that matters:

the loop stops on **evidence**, not on a bare promise, and not by accident. We drive the real hook
(no mocks) with the Cursor `text` schema and a real scratchpad on disk, and assert each exit path:

  • promise + evidence            → STOP (state cleaned up)        ← the success exit
  • promise WITHOUT evidence       → CONTINUE (re-feed, ignored)   ← the anti-false-done guard
  • promise + evidence, AC pending → CONTINUE (re-feed, ignored)   ← the anti-DRIFT anchor gate
  • promise + evidence, ACs done   → STOP                          ← anchor satisfied
  • no promise, under cap          → CONTINUE (iteration bumped)
  • iteration >= max_iterations    → STOP by cap                   ← distinct from the evidence exit
  • .orchestrator/STOP signal      → STOP immediately

This is the "stopped by evidence, not by cap" proof: cases 1 and 4 stop for *different* reasons,
and case 2 proves a promise alone never escapes the loop.
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO, "hooks", "loop_stop.py")

SCRATCHPAD = """---
iteration: {iteration}
max_iterations: {max_iter}
completion_promise: "SIMPLICIO_DONE"
evidence_required: true
started_at: "2026-06-24T00:00:00Z"
---
Implement the thing and prove it works.
"""


def _arm(root, iteration=1, max_iter=5):
    loop = os.path.join(root, ".orchestrator", "loop")
    os.makedirs(loop, exist_ok=True)
    with open(os.path.join(loop, "scratchpad.md"), "w", encoding="utf-8") as f:
        f.write(SCRATCHPAD.format(iteration=iteration, max_iter=max_iter))
    return loop


def _tick(root, response_text):
    """Run loop_stop.py exactly as the host would: cwd=root, stdin = {text:...}."""
    return subprocess.run([sys.executable, HOOK], input=json.dumps({"text": response_text}),
                          capture_output=True, text=True, cwd=root)


def _scratchpad(root):
    return os.path.join(root, ".orchestrator", "loop", "scratchpad.md")


def _iteration(root):
    with open(_scratchpad(root), encoding="utf-8") as f:
        for line in f:
            if line.startswith("iteration:"):
                return int(line.split(":", 1)[1])
    return None


def test_promise_with_evidence_stops(tmp_path):
    root = str(tmp_path)
    _arm(root)
    r = _tick(root, "All green. <promise>SIMPLICIO_DONE</promise> tests pass ✓ "
                    "https://github.com/o/r/pull/9")
    assert r.returncode == 0
    assert r.stdout.strip() == "", "expected STOP (no re-feed), got: %s" % r.stdout
    assert not os.path.exists(_scratchpad(root)), "state should be cleaned up on a verified stop"


def test_bare_promise_without_evidence_continues(tmp_path):
    root = str(tmp_path)
    _arm(root, iteration=1, max_iter=5)
    r = _tick(root, "I think I'm done. <promise>SIMPLICIO_DONE</promise>")  # no evidence token
    assert r.returncode == 0
    # the loop must NOT stop on a bare promise — it re-feeds and bumps the iteration
    assert "followup_message" in r.stdout or "block" in r.stdout, \
        "a bare promise must be ignored, not honored:\n%s" % r.stdout
    assert os.path.exists(_scratchpad(root)), "loop wrongly stopped on a bare promise"
    assert _iteration(root) == 2


def _write_anchor(root, criteria):
    loop = os.path.join(root, ".orchestrator", "loop")
    os.makedirs(loop, exist_ok=True)
    with open(os.path.join(loop, "anchor.json"), "w", encoding="utf-8") as f:
        json.dump({"item": "1", "goal": "g", "goal_fp": "x", "criteria": criteria}, f)


def test_promise_with_evidence_but_pending_anchor_continues(tmp_path):
    # The mechanical anti-drift gate: even WITH evidence, a promise must NOT stop the loop while the
    # task anchor still has an unverified acceptance criterion — it re-feeds instead, naming the gap.
    root = str(tmp_path)
    _arm(root, iteration=1, max_iter=5)
    _write_anchor(root, [{"id": "AC1", "status": "done"}, {"id": "AC2", "status": "pending"}])
    r = _tick(root, "Looks done. <promise>SIMPLICIO_DONE</promise> tests pass ✓ "
                    "https://github.com/o/r/pull/9")
    assert r.returncode == 0
    assert "followup_message" in r.stdout or "block" in r.stdout, \
        "a promise with an open AC must be ignored, not honored:\n%s" % r.stdout
    assert os.path.exists(_scratchpad(root)), "loop wrongly stopped with an open AC"
    assert "AC2" in r.stdout, "re-feed should name the open acceptance criterion:\n%s" % r.stdout


def test_promise_with_evidence_all_acs_done_stops(tmp_path):
    # Once every anchored AC is verified, the evidence-backed promise stops the loop as before.
    root = str(tmp_path)
    _arm(root, iteration=1, max_iter=5)
    _write_anchor(root, [{"id": "AC1", "status": "done"}, {"id": "AC2", "status": "done"}])
    r = _tick(root, "All green. <promise>SIMPLICIO_DONE</promise> tests pass ✓ "
                    "https://github.com/o/r/pull/9")
    assert r.returncode == 0
    assert r.stdout.strip() == "", "expected STOP (every AC verified), got: %s" % r.stdout
    assert not os.path.exists(_scratchpad(root)), "state should be cleaned up on a verified stop"


def test_no_promise_continues_and_bumps_iteration(tmp_path):
    root = str(tmp_path)
    _arm(root, iteration=2, max_iter=5)
    r = _tick(root, "Made progress; still working on the failing test.")
    assert "followup_message" in r.stdout
    assert _iteration(root) == 3


def test_iteration_cap_stops(tmp_path):
    root = str(tmp_path)
    _arm(root, iteration=5, max_iter=5)  # at the cap
    r = _tick(root, "still going, no promise here")
    assert r.stdout.strip() == "", "cap reached must STOP, not re-feed:\n%s" % r.stdout
    assert not os.path.exists(_scratchpad(root)), "cap stop should clean up state"


def test_stop_signal_halts(tmp_path):
    root = str(tmp_path)
    _arm(root)
    open(os.path.join(root, ".orchestrator", "STOP"), "w").close()
    r = _tick(root, "anything")
    assert r.stdout.strip() == ""
    assert not os.path.exists(_scratchpad(root))


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_loop_e2e")
