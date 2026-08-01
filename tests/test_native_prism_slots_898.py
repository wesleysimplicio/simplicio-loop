from __future__ import annotations

from simplicio_loop.runner import _build_native_prism_scheduler


def test_native_prism_partitions_independent_impacts_into_real_slots() -> None:
    items = [
        {"run_id": "run", "task_id": "a", "repo": "repo", "task_spec": {"files_affected": ["a.py"]}},
        {"run_id": "run", "task_id": "b", "repo": "repo", "task_spec": {"files_affected": ["b.py"]}},
    ]
    scheduler, _, _ = _build_native_prism_scheduler(items, worker_limit=2)
    snapshot = scheduler.snapshot()
    assert len(snapshot["slots"]) == 2
    assert 1 <= snapshot["policy"]["global_worker_limit"] <= 2
    assert snapshot["policy"]["max_tasks_per_slot"] == 10
