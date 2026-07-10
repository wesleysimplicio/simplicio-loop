import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from simplicio_loop.drain import evaluate_drain


def _task(task_id="T1", state="done", delivered=True, evidence=None):
    return {
        "id": task_id,
        "state": state,
        "delivery_satisfied": delivered,
        "evidence": evidence or {
            "watcher_status": "MEASURED",
            "watcher_match": True,
            "oracle_verdict": "COMPLETE",
            "fresh": True,
            "checked_at": "2026-07-10T20:00:00Z",
            "contract_hash": "contract-T1",
            "receipt_id": "receipt-T1",
            "challenge": "challenge-1",
        },
    }


def _snapshot(tasks, polls=("empty:1", "empty:1"), leases=0):
    return {"tasks": tasks, "polls": list(polls), "active_leases": leases, "challenge": "challenge-1"}


def test_drain_requires_two_identical_empty_polls():
    result = evaluate_drain(_snapshot([_task()]))
    assert result["verdict"] == "DRAINED"
    assert result["tag"] == "MEASURED"


def test_late_arrival_or_changed_source_keeps_queue_open():
    result = evaluate_drain(_snapshot([_task()], polls=("empty:1", "empty:2")))
    assert result["verdict"] == "CONTINUE"
    assert result["reason_code"] == "source_not_quiet"


def test_identical_non_empty_polls_cannot_drain():
    result = evaluate_drain(_snapshot([_task()], polls=({"ready": 1}, {"ready": 1})))
    assert result["reason_code"] == "source_not_quiet"


def test_active_lease_blocks_drain_even_when_tasks_are_done():
    result = evaluate_drain(_snapshot([_task()], leases=1))
    assert result["reason_code"] == "leases_active"


def test_ready_blocked_dead_letter_and_running_tasks_never_count_as_drained():
    for state in ("ready", "blocked", "dead-letter", "running"):
        result = evaluate_drain(_snapshot([_task(state=state)]))
        assert result["verdict"] == "CONTINUE"
        assert result["reason_code"] == "tasks_pending"


def test_done_task_requires_measured_watcher_oracle_and_delivery():
    stale = _task(evidence={"watcher_status": "UNVERIFIED", "watcher_match": False, "oracle_verdict": "CONTINUE"})
    result = evaluate_drain(_snapshot([stale]))
    assert result["reason_code"] == "evidence_pending"

    unbound = _task(evidence={
        "watcher_status": "MEASURED", "watcher_match": True, "oracle_verdict": "COMPLETE",
        "fresh": True, "checked_at": "2026-07-10T20:00:00Z", "contract_hash": "x",
    })
    result = evaluate_drain(_snapshot([unbound]))
    assert result["reason_code"] == "evidence_pending"

    undelivered = _task(delivered=False)
    result = evaluate_drain(_snapshot([undelivered]))
    assert result["reason_code"] == "evidence_pending"


def test_unknown_task_state_is_fail_closed():
    result = evaluate_drain(_snapshot([_task(state="wat")]))
    assert result["reason_code"] == "task_state_unknown"
