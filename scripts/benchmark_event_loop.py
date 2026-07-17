#!/usr/bin/env python
"""Small reproducible event-loop benchmark for rollout evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simplicio_loop.event_loop import configure_event_loop


async def _noop() -> None:
    await asyncio.sleep(0)


def benchmark(iterations: int = 1000) -> Dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be positive")
    selection = configure_event_loop()
    samples: List[float] = []
    started = time.perf_counter()
    for _ in range(iterations):
        tick = time.perf_counter()
        asyncio.run(_noop())
        samples.append((time.perf_counter() - tick) * 1000)
    elapsed = time.perf_counter() - started
    return {
        "schema": "simplicio.event-loop-benchmark/v1",
        "selection": selection.as_dict(),
        "iterations": iterations,
        "elapsed_seconds": elapsed,
        "throughput_per_second": iterations / elapsed if elapsed else 0.0,
        "p95_ms": statistics.quantiles(samples, n=20)[-1] if len(samples) >= 20 else max(samples),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1000)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.iterations), sort_keys=True))


if __name__ == "__main__":
    main()
