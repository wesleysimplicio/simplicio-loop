#!/usr/bin/env python
"""Benchmark admitted (governed) vs unthrottled (ungoverned) task dispatch.

Compares CPU, RSS, throughput and p95 latency for a concurrent workload run
through ``ResourceGovernor.admit``/``release`` against the same workload with
no admission control at all, so a regression in governor overhead is
observable in bench/hub-governor-benchmark.json.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simplicio_loop.hub_governor import ResourceGovernor, ResourceLimits, ResourceRequest, ResourceThrottled

try:
    import resource
except ImportError:
    resource = None


def _rss_mb() -> Optional[float]:
    if resource is None:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024.0 * 1024.0) if sys.platform == "darwin" else peak / 1024.0


def _work_unit() -> None:
    total = 0
    for value in range(2000):
        total += value * value
    _ = total


def _governed_task(governor: ResourceGovernor, index: int) -> float:
    started = time.perf_counter()
    try:
        lease = governor.admit(f"client-{index % 4}", f"task-{index}", ResourceRequest(cpu=1))
    except ResourceThrottled:
        return (time.perf_counter() - started) * 1000.0
    try:
        _work_unit()
    finally:
        governor.release(lease)
    return (time.perf_counter() - started) * 1000.0


def _ungoverned_task(_: int) -> float:
    started = time.perf_counter()
    _work_unit()
    return (time.perf_counter() - started) * 1000.0


def _run(tasks: int, workers: int, governed: bool) -> Dict[str, Any]:
    governor = ResourceGovernor(ResourceLimits(cpu=workers)) if governed else None
    cpu_started = time.process_time()
    started = time.perf_counter()
    samples: List[float] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        if governed:
            assert governor is not None
            futures = [pool.submit(_governed_task, governor, index) for index in range(tasks)]
        else:
            futures = [pool.submit(_ungoverned_task, index) for index in range(tasks)]
        for future in futures:
            samples.append(future.result())
    elapsed = time.perf_counter() - started
    cpu_seconds = time.process_time() - cpu_started
    return {
        "elapsed_seconds": elapsed,
        "cpu_seconds": cpu_seconds,
        "cpu_percent": cpu_seconds / elapsed * 100.0 if elapsed else 0.0,
        "throughput_per_second": tasks / elapsed if elapsed else 0.0,
        "p95_ms": statistics.quantiles(samples, n=20)[-1] if len(samples) >= 20 else max(samples),
        "throttle_receipts": len(governor.receipts()) if governor else 0,
    }


def benchmark(tasks: int = 200, workers: int = 8) -> Dict[str, Any]:
    if tasks < 1 or workers < 1:
        raise ValueError("tasks and workers must be positive")
    governed = _run(tasks, workers, governed=True)
    peak_rss_mb = _rss_mb()
    ungoverned = _run(tasks, workers, governed=False)
    return {
        "schema": "simplicio.hub-governor-benchmark/v1",
        "tasks": tasks,
        "workers": workers,
        "governed": governed,
        "ungoverned": ungoverned,
        "peak_rss_mb": peak_rss_mb,
        "rss_source": "resource.getrusage" if resource is not None else "unavailable",
        "overhead_percent": (
            (governed["elapsed_seconds"] - ungoverned["elapsed_seconds"])
            / ungoverned["elapsed_seconds"]
            * 100.0
            if ungoverned["elapsed_seconds"]
            else 0.0
        ),
    }


def main(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=int, default=200)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    receipt = benchmark(args.tasks, args.workers)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        receipt["output"] = str(args.output)
    print(json.dumps(receipt, sort_keys=True))
    return receipt


if __name__ == "__main__":
    main()
