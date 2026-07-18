"""Throughput/latency benchmark for HubRetryQueue submit/claim/complete under load (#504)."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simplicio_loop.hub_queue_retry import HubRetryQueue


def _percentile(samples: List[float], pct: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(round(pct / 100.0 * len(ordered))) - 1))
    return ordered[index]


def benchmark(tasks: int = 500, workers: int = 1) -> Dict[str, Any]:
    if tasks < 1 or workers < 1:
        raise ValueError("tasks and workers must be positive")
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "queue.db")
        queue = HubRetryQueue(path)

        submit_samples: List[float] = []
        started = time.perf_counter()
        for i in range(tasks):
            tick = time.perf_counter()
            queue.submit({"i": i}, idempotency_key="task-%d" % i)
            submit_samples.append((time.perf_counter() - tick) * 1000.0)
        submit_elapsed = time.perf_counter() - started

        claim_samples: List[float] = []
        complete_samples: List[float] = []
        claim_started = time.perf_counter()
        claimed = 0
        while claimed < tasks:
            tick = time.perf_counter()
            lease = queue.claim("bench-worker", ttl=30)
            claim_samples.append((time.perf_counter() - tick) * 1000.0)
            if lease is None:
                break
            claimed += 1
            tick = time.perf_counter()
            queue.complete(lease)
            complete_samples.append((time.perf_counter() - tick) * 1000.0)
        claim_complete_elapsed = time.perf_counter() - claim_started
        queue.close()

        return {
            "schema": "simplicio.hub-queue-retry-benchmark/v1",
            "tasks": tasks,
            "claimed": claimed,
            "submit": {
                "elapsed_seconds": submit_elapsed,
                "throughput_per_second": tasks / submit_elapsed if submit_elapsed else 0.0,
                "p50_ms": statistics.median(submit_samples) if submit_samples else 0.0,
                "p95_ms": _percentile(submit_samples, 95),
            },
            "claim": {
                "p50_ms": statistics.median(claim_samples) if claim_samples else 0.0,
                "p95_ms": _percentile(claim_samples, 95),
            },
            "complete": {
                "p50_ms": statistics.median(complete_samples) if complete_samples else 0.0,
                "p95_ms": _percentile(complete_samples, 95),
            },
            "claim_complete_cycle": {
                "elapsed_seconds": claim_complete_elapsed,
                "throughput_per_second": claimed / claim_complete_elapsed if claim_complete_elapsed else 0.0,
            },
        }


def main(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=int, default=500)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    receipt = benchmark(args.tasks)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        receipt["output"] = str(args.output)
    print(json.dumps(receipt, sort_keys=True))
    return receipt


if __name__ == "__main__":
    main()
