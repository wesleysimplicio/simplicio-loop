#!/usr/bin/env python
"""Reproducible event-loop benchmark with workload, CPU/RSS, and rollout receipts."""

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

from simplicio_loop.event_loop import configure_event_loop

try:
    import resource
except ImportError:
    resource = None


async def _noop() -> None:
    await asyncio.sleep(0)


async def _workload(name: str) -> None:
    if name == "noop":
        await _noop()
        return
    if name == "gather":
        await asyncio.gather(*(_noop() for _ in range(8)))
        return
    raise ValueError("unknown workload: %s" % name)


def _peak_rss_mb() -> Optional[float]:
    if resource is not None:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            return peak / (1024.0 * 1024.0)
        return peak / 1024.0
    if tracemalloc.is_tracing():
        return tracemalloc.get_traced_memory()[1] / (1024.0 * 1024.0)
    return None


def benchmark(iterations: int = 1000, workload: str = "noop") -> Dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be positive")
    if workload not in {"noop", "gather"}:
        raise ValueError("workload must be noop or gather")
    selection = configure_event_loop()
    tracing_started = False
    if resource is None and not tracemalloc.is_tracing():
        tracemalloc.start()
        tracing_started = True
    samples: List[float] = []
    started = time.perf_counter()
    cpu_started = time.process_time()
    for _ in range(iterations):
        tick = time.perf_counter()
        asyncio.run(_workload(workload))
        samples.append((time.perf_counter() - tick) * 1000)
    elapsed = time.perf_counter() - started
    cpu_seconds = time.process_time() - cpu_started
    peak_rss_mb = _peak_rss_mb()
    if tracing_started:
        tracemalloc.stop()
    return {
        "schema": "simplicio.event-loop-benchmark/v1",
        "selection": selection.as_dict(),
        "iterations": iterations,
        "workload": workload,
        "elapsed_seconds": elapsed,
        "cpu_seconds": cpu_seconds,
        "cpu_percent": (cpu_seconds / elapsed * 100.0) if elapsed else 0.0,
        "peak_rss_mb": peak_rss_mb,
        "rss_source": "resource.getrusage" if resource is not None else "tracemalloc",
        "throughput_per_second": iterations / elapsed if elapsed else 0.0,
        "p95_ms": statistics.quantiles(samples, n=20)[-1] if len(samples) >= 20 else max(samples),
    }


def main(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--workload", choices=("noop", "gather"), default="noop")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    receipt = benchmark(args.iterations, args.workload)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        receipt["output"] = str(args.output)
    print(json.dumps(receipt, sort_keys=True))
    return receipt


if __name__ == "__main__":
    main()
