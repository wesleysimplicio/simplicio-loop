"""Queue overhead matrix for issue #889."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from simplicio_loop.local_task_queue import LocalTaskQueue


def main() -> int:
    rows = []
    for size in (1, 10, 100, 1000):
        with tempfile.TemporaryDirectory() as temporary:
            queue = LocalTaskQueue(Path(temporary))
            started = time.perf_counter_ns()
            for index in range(size):
                queue.submit(f"task-{index}")
            enqueue_ns = time.perf_counter_ns() - started
            claim_started = time.perf_counter_ns()
            for index in range(size):
                queue.claim_local(f"task-{index}", "bench", idempotency_key=f"key-{index}")
            claim_ns = time.perf_counter_ns() - claim_started
            rows.append({"queued_tasks": size, "enqueue_ns": enqueue_ns,
                         "claim_ns": claim_ns, "db_bytes": Path(queue.path).stat().st_size})
    print(json.dumps({"schema": "simplicio.loop.local-task-queue-benchmark/v1", "rows": rows},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
