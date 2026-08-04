from __future__ import annotations

from simplicio_loop.runner import _build_native_prism_scheduler


def test_native_prism_partitions_independent_impacts_into_real_slots() -> None:
    items = [
        {"run_id": "run", "task_id": "a", "repo": "repo", "task_spec": {"files_affected": ["a.py"]}},
        {"run_id": "run", "task_id": "b", "repo": "repo", "task_spec": {"files_affected": ["b.py"]}},
    ]
    scheduler, _, capacity = _build_native_prism_scheduler(items, worker_limit=2)
    snapshot = scheduler.snapshot()
    assert len(snapshot["slots"]) == 2
    assert 1 <= snapshot["policy"]["global_worker_limit"] <= 2
    assert snapshot["policy"]["max_tasks_per_slot"] == 10


def test_native_prism_persists_governor_receipt_and_refreshes_capacity() -> None:
    items = [
        {"run_id": "run", "task_id": "a", "repo": ".", "task_spec": {"slot_key": "a"}},
        {"run_id": "run", "task_id": "b", "repo": ".", "task_spec": {"slot_key": "b"}},
    ]
    scheduler, _, capacity = _build_native_prism_scheduler(items, worker_limit=3)

    assert capacity["budget_governor"]["schema"] == "simplicio.prism-budget-status/v1"
    assert capacity["budget_governor"]["events"][0]["reason_code"] == "INITIAL_SAMPLE"
    assert capacity["policy"]["recovery_reserve"] == 1
    assert capacity["policy"]["validation_reserve"] == 1
    assert "cpu_millis" in capacity["budget_governor"]["events"][0]["null_reasons"]

    scheduler.native_capacity_refresh()
    assert len(capacity["budget_governor"]["events"]) == 2
    assert capacity["budget_governor"]["events"][1]["reason_code"] == "STABLE"


def test_native_prism_has_no_logical_slot_overflow_or_capacity_ceiling() -> None:
    items = [
        {
            "run_id": "run",
            "task_id": f"task-{index}",
            "repo": ".",
            "task_spec": {"slot_key": f"partition-{index}"},
        }
        for index in range(25)
    ]
    scheduler, _, _ = _build_native_prism_scheduler(items, worker_limit=25)
    snapshot = scheduler.snapshot()
    assert snapshot["metrics"]["logical_slots"] == 25
    assert "overflow" not in {slot["supervisor_agent"] for slot in snapshot["slots"]}

    same_slot_items = [
        {"run_id": "run", "task_id": f"task-{index}", "repo": ".",
         "task_spec": {"slot_key": "shared"}}
        for index in range(25)
    ]
    scheduler, _, _ = _build_native_prism_scheduler(same_slot_items, worker_limit=25)
    assert scheduler.slots[next(iter(scheduler.slots))].capacity == 25
