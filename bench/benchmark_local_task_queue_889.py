"""Queue overhead matrix for issue #889."""

from __future__ import annotations

import json
import tempfile
import time
import argparse
from pathlib import Path

from simplicio_loop.local_task_queue import LocalTaskQueue


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-enqueue-us", type=float, default=30000.0)
    parser.add_argument("--max-claim-us", type=float, default=30000.0)
    parser.add_argument(
        "--sizes", default="1,10,100,1000",
        help="comma-separated task counts (default: 1,10,100,1000)",
    )
    args = parser.parse_args(argv)
    sizes = tuple(int(value.strip()) for value in args.sizes.split(",") if value.strip())
    if not sizes or any(value < 1 for value in sizes):
        parser.error("--sizes must contain positive integers")
    rows = []
    for size in sizes:
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
    thresholds = {
        "enqueue": max(row["enqueue_ns"] / row["queued_tasks"] / 1000 for row in rows) <= args.max_enqueue_us,
        "claim": max(row["claim_ns"] / row["queued_tasks"] / 1000 for row in rows) <= args.max_claim_us,
    }
    print(json.dumps({"schema": "simplicio.loop.local-task-queue-benchmark/v1", "rows": rows,
                      "thresholds": thresholds},
                     sort_keys=True))
    return 0 if all(thresholds.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
