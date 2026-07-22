"""Throughput/latency/fairness benchmark for FairScheduler under heavy+light load (#505)."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import platform
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simplicio_loop.hub_scheduler import FairScheduler, ScheduledJob

try:
    import resource
except ImportError:  # pragma: no cover - resource is POSIX-only
    resource = None  # type: ignore[assignment]


def _rss_mb() -> Optional[float]:
    if resource is None:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is bytes on macOS, KB on Linux.
    return peak / (1024.0 * 1024.0) if sys.platform == "darwin" else peak / 1024.0


def _percentile(samples: List[float], pct: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(round(pct / 100.0 * len(ordered))) - 1))
    return ordered[index]


def benchmark(heavy_jobs: int = 500, light_jobs: int = 100) -> Dict[str, Any]:
    if heavy_jobs < 1 or light_jobs < 1:
        raise ValueError("heavy_jobs and light_jobs must be positive")
    scheduler = FairScheduler(max_inflight_per_client=1000, quantum=1)
    enqueued_at: Dict[str, float] = {}
    for index in range(heavy_jobs):
        task_id = f"heavy-{index}"
        enqueued_at[task_id] = time.perf_counter()
        scheduler.enqueue(ScheduledJob(task_id, "heavy"))
    for index in range(light_jobs):
        task_id = f"light-{index}"
        enqueued_at[task_id] = time.perf_counter()
        scheduler.enqueue(ScheduledJob(task_id, "light"))

    total = heavy_jobs + light_jobs
    dispatch_samples: List[float] = []
    served = {"heavy": 0, "light": 0}
    queue_wait_samples: List[float] = []
    started = time.perf_counter()
    for _ in range(total):
        tick = time.perf_counter()
        job = scheduler.next()
        dispatch_samples.append((time.perf_counter() - tick) * 1000.0)
        if job is None:
            break
        queue_wait_samples.append((time.perf_counter() - enqueued_at[job.task_id]) * 1000.0)
        served[job.client_id] += 1
        scheduler.complete(job.task_id)
    elapsed = time.perf_counter() - started

    status = scheduler.status()
    return {
        "schema": "simplicio.hub-scheduler-benchmark/v1",
        "heavy_jobs": heavy_jobs,
        "light_jobs": light_jobs,
        "served": served,
        "elapsed_seconds": elapsed,
        "throughput_per_second": total / elapsed if elapsed else 0.0,
        "dispatch_p50_ms": statistics.median(dispatch_samples) if dispatch_samples else 0.0,
        "dispatch_p95_ms": _percentile(dispatch_samples, 95),
        "dispatch_p99_ms": _percentile(dispatch_samples, 99),
        "queue_wait_p95_ms": _percentile(queue_wait_samples, 95),
        "queue_wait_p99_ms": _percentile(queue_wait_samples, 99),
        "jains_fairness_index": status["jains_fairness_index"],
        "starvation_preventions": status["starvation_preventions"],
        "peak_rss_mb": _rss_mb(),
        "rss_source": "resource.getrusage" if resource is not None else "unavailable",
        "environment": {"python": sys.version.split()[0], "platform": platform.platform()},
        "commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=False
        ).stdout.strip() or None,
    }


def main(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--heavy-jobs", type=int, default=500)
    parser.add_argument("--light-jobs", type=int, default=100)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    receipt = benchmark(args.heavy_jobs, args.light_jobs)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        receipt["output"] = str(args.output)
    print(json.dumps(receipt, sort_keys=True))
    return receipt


if __name__ == "__main__":
    main()
