#!/usr/bin/env python
"""Benchmark AsyncProcessSupervisor: throughput, p95 latency, CPU, RSS.

Closes the "Regressao, stress de restart e benchmark throughput/p95/CPU/RSS"
acceptance criterion tracked in issue #509 for the bounded async process
supervisor added in PR #529 (simplicio_loop/async_io_supervisor.py).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simplicio_loop.async_io_supervisor import AsyncProcessSupervisor
from simplicio_loop.process_supervisor import ProcessSpec

try:
    import resource
except ImportError:
    resource = None


def _spec(index: int) -> ProcessSpec:
    return ProcessSpec(
        argv=(sys.executable, "-c", f"print({index})"),
        timeout_seconds=5.0,
        idempotency_key=f"bench-{index}",
    )


async def _run_batch(items: int, max_concurrency: int) -> Dict[str, Any]:
    supervisor = AsyncProcessSupervisor(max_concurrency=max_concurrency)
    latencies: List[float] = []

    async def one(index: int) -> None:
        tick = time.perf_counter()
        result = await supervisor.run(_spec(index))
        latencies.append((time.perf_counter() - tick) * 1000.0)
        if result.returncode != 0:
            raise RuntimeError(f"benchmark process {index} failed: {result.to_dict()}")

    await asyncio.gather(*(one(i) for i in range(items)))
    status = supervisor.status()
    return {"status": status, "latencies_ms": latencies}


def _rss_mb() -> Optional[float]:
    if resource is not None:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / (1024.0 * 1024.0) if sys.platform == "darwin" else peak / 1024.0
    if tracemalloc.is_tracing():
        return tracemalloc.get_traced_memory()[1] / (1024.0 * 1024.0)
    return None


def benchmark(items: int = 20, max_concurrency: int = 4, repeats: int = 3) -> Dict[str, Any]:
    if items < 1 or max_concurrency < 1 or repeats < 1:
        raise ValueError("items, max_concurrency and repeats must be positive")

    tracing_started = False
    if resource is None and not tracemalloc.is_tracing():
        tracemalloc.start()
        tracing_started = True

    all_latencies: List[float] = []
    active_leases_after: List[int] = []
    active_tasks_after: List[int] = []
    cpu_started = time.process_time()
    started = time.perf_counter()
    for _ in range(repeats):
        batch = asyncio.run(_run_batch(items, max_concurrency))
        all_latencies.extend(batch["latencies_ms"])
        active_leases_after.append(batch["status"]["active_leases"])
        active_tasks_after.append(batch["status"]["active_tasks"])
    elapsed = time.perf_counter() - started
    cpu_seconds = time.process_time() - cpu_started
    peak_rss_mb = _rss_mb()
    if tracing_started:
        tracemalloc.stop()

    total = items * repeats
    p95 = (
        statistics.quantiles(all_latencies, n=20)[-1]
        if len(all_latencies) >= 20
        else max(all_latencies)
    )
    return {
        "schema": "simplicio.async-process-supervisor-benchmark/v1",
        "items": items,
        "max_concurrency": max_concurrency,
        "repeats": repeats,
        "total_processes": total,
        "elapsed_seconds": elapsed,
        "cpu_seconds": cpu_seconds,
        "cpu_percent": cpu_seconds / elapsed * 100.0 if elapsed else 0.0,
        "peak_rss_mb": peak_rss_mb,
        "rss_source": "resource.getrusage" if resource is not None else "tracemalloc",
        "throughput_per_second": total / elapsed if elapsed else 0.0,
        "p95_ms": p95,
        "no_leak": all(count == 0 for count in active_leases_after + active_tasks_after),
    }


def main(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--items", type=int, default=20)
    parser.add_argument("--max-concurrency", type=int, default=4)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    receipt = benchmark(args.items, args.max_concurrency, args.repeats)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        receipt["output"] = str(args.output)
    print(json.dumps(receipt, sort_keys=True))
    return receipt


if __name__ == "__main__":
    main()
