import os
import sys
from concurrent.futures import ThreadPoolExecutor

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import pytest

import simplicio_loop.drain as drain_module
from simplicio_loop.drain import (
    DrainReceiptError,
    evaluate_drain,
    load_drain_receipt,
    persist_drain_receipt,
)


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


def test_persisted_receipt_is_atomic_and_idempotent(tmp_path):
    path = tmp_path / "drain-receipt.json"
    snapshot = _snapshot([_task()])

    first = persist_drain_receipt(path, snapshot)
    second = persist_drain_receipt(path, snapshot)

    assert second == first
    assert load_drain_receipt(path) == first
    assert path.read_bytes().endswith(b"\n")
    assert not list(tmp_path.glob(".drain-receipt.json.*.tmp"))


def test_concurrent_verifiers_leave_one_valid_receipt(tmp_path):
    path = tmp_path / "drain-receipt.json"
    snapshot = _snapshot([_task()])

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: persist_drain_receipt(path, snapshot), range(24)))

    assert all(item == results[0] for item in results)
    assert load_drain_receipt(path) == results[0]


def test_late_arrival_replaces_old_receipt_atomically(tmp_path):
    path = tmp_path / "drain-receipt.json"
    drained = persist_drain_receipt(path, _snapshot([_task()]))
    changed = persist_drain_receipt(path, _snapshot([_task()], polls=("empty:1", "ready:1")))

    assert drained["verdict"] == "DRAINED"
    assert changed["verdict"] == "CONTINUE"
    assert load_drain_receipt(path)["reason_code"] == "source_not_quiet"


def test_corrupt_existing_receipt_fails_closed(tmp_path):
    path = tmp_path / "drain-receipt.json"
    path.write_text('{"schema":"simplicio.drain-receipt/v1"', encoding="utf-8")

    with pytest.raises(DrainReceiptError):
        persist_drain_receipt(path, _snapshot([_task()]))


def test_persistence_has_self_contained_lock_fallback(tmp_path, monkeypatch):
    path = tmp_path / "drain-receipt.json"
    monkeypatch.setattr(drain_module, "_locks", None)

    receipt = persist_drain_receipt(path, _snapshot([_task()]))

    assert load_drain_receipt(path) == receipt
