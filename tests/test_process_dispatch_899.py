from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor

from simplicio_loop.runner import _run_operator_item_process


def test_operator_lane_runs_in_importable_child_process() -> None:
    item = {
        "repo": ".",
        "run_id": "synthetic-process-899",
        "task_index": 1,
        "worker_id": "worker-899",
        "task_id": "task-899",
    }
    with ProcessPoolExecutor(max_workers=1) as pool:
        records = pool.submit(_run_operator_item_process, item, 0).result(timeout=60)
    assert len(records) == 1
    assert records[0]["status"] == "failed"
    assert records[0]["retry_scope"] == "worker-process"
