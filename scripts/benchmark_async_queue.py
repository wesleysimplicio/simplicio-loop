#!/usr/bin/env python
"""Benchmark the event-driven bounded queue and its idle path."""

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

from simplicio_loop.async_bounded_queue import AsyncBoundedQueue, QueueClosed

try:
    import resource
except ImportError:
    resource = None


async def _drain(items: int, capacity: int) -> Dict[str, Any]:
    queue = AsyncBoundedQueue(capacity, overload="wait")

    async def produce() -> None:
        for value in range(items):
            await queue.put(value)

    async def consume() -> None:
        for _ in range(items):
            await queue.get()
            queue.task_done()

    await asyncio.gather(produce(), consume())
    await queue.join()
    return queue.status()


async def _idle_wait(seconds: float) -> None:
    queue = AsyncBoundedQueue(1)
    waiter = asyncio.create_task(queue.get())
    await asyncio.sleep(seconds)
    await queue.close()
    try:
        await waiter
    except QueueClosed:
        pass


def _rss_mb() -> Optional[float]:
    if resource is not None:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / (1024.0 * 1024.0) if sys.platform == "darwin" else peak / 1024.0
    if tracemalloc.is_tracing():
        return tracemalloc.get_traced_memory()[1] / (1024.0 * 1024.0)
    return None


def benchmark(items: int = 100, capacity: int = 4, repeats: int = 5) -> Dict[str, Any]:
    if items < 1 or capacity < 1 or repeats < 1:
        raise ValueError("items, capacity and repeats must be positive")
    tracing_started = False
    if resource is None and not tracemalloc.is_tracing():
        tracemalloc.start()
        tracing_started = True
    samples: List[float] = []
    cpu_started = time.process_time()
    started = time.perf_counter()
    status: Dict[str, Any] = {}
    for _ in range(repeats):
        tick = time.perf_counter()
        status = asyncio.run(_drain(items, capacity))
        samples.append((time.perf_counter() - tick) * 1000.0)
    elapsed = time.perf_counter() - started
    cpu_seconds = time.process_time() - cpu_started
    idle_cpu_started = time.process_time()
    idle_started = time.perf_counter()
    asyncio.run(_idle_wait(0.01))
    idle_elapsed = time.perf_counter() - idle_started
    idle_cpu_seconds = time.process_time() - idle_cpu_started
    peak_rss_mb = _rss_mb()
    if tracing_started:
        tracemalloc.stop()
    return {
        "schema": "simplicio.async-queue-benchmark/v1",
        "items": items,
        "capacity": capacity,
        "repeats": repeats,
        "elapsed_seconds": elapsed,
        "cpu_seconds": cpu_seconds,
        "cpu_percent": cpu_seconds / elapsed * 100.0 if elapsed else 0.0,
        "idle_elapsed_seconds": idle_elapsed,
        "idle_cpu_percent": idle_cpu_seconds / idle_elapsed * 100.0 if idle_elapsed else 0.0,
        "peak_rss_mb": peak_rss_mb,
        "rss_source": "resource.getrusage" if resource is not None else "tracemalloc",
        "throughput_per_second": (items * repeats) / elapsed if elapsed else 0.0,
        "p95_ms": statistics.quantiles(samples, n=20)[-1] if len(samples) >= 20 else max(samples),
        "queue": status,
    }


def main(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--items", type=int, default=100)
    parser.add_argument("--capacity", type=int, default=4)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    receipt = benchmark(args.items, args.capacity, args.repeats)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        receipt["output"] = str(args.output)
    print(json.dumps(receipt, sort_keys=True))
    return receipt


if __name__ == "__main__":
    main()
