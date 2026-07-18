#!/usr/bin/env python3
"""Measure real supervised subprocess recreation throughput and resource cost."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simplicio_loop.async_io_supervisor import AsyncProcessSupervisor
from simplicio_loop.process_supervisor import ProcessSpec


def _rss_bytes() -> int | None:
    try:
        import psutil
        return int(psutil.Process().memory_info().rss)
    except (ImportError, OSError):
        return None


async def _run(rounds: int, concurrency: int) -> Dict[str, Any]:
    durations: list[float] = []
    outcomes: list[str] = []
    started = time.perf_counter()
    for round_index in range(rounds):
        supervisor = AsyncProcessSupervisor(max_concurrency=concurrency)
        specs = [
            ProcessSpec(
                argv=(sys.executable, "-c", f"print('round-{round_index}-job-{job_index}')"),
                timeout_seconds=5,
                idempotency_key=f"round-{round_index}-job-{job_index}",
            )
            for job_index in range(concurrency)
        ]
        batch_started = time.perf_counter()
        results = await asyncio.gather(*(supervisor.run(item) for item in specs))
        durations.append(time.perf_counter() - batch_started)
        outcomes.extend(result.stdout.strip() for result in results)
        await supervisor.shutdown()
    elapsed = time.perf_counter() - started
    sorted_durations = sorted(durations)
    p95_index = max(0, min(len(sorted_durations) - 1, int(len(sorted_durations) * 0.95) - 1))
    return {
        "schema": "simplicio.async-process-supervisor-benchmark/v1",
        "rounds": rounds,
        "concurrency": concurrency,
        "processes": rounds * concurrency,
        "throughput_processes_per_second": (rounds * concurrency) / elapsed if elapsed else 0.0,
        "batch_p95_seconds": sorted_durations[p95_index],
        "cpu_process_seconds": time.process_time(),
        "rss_bytes": _rss_bytes(),
        "rss_source": "psutil" if _rss_bytes() is not None else "unavailable",
        "unique_outcomes": len(set(outcomes)),
        "duplicate_outcomes": len(outcomes) - len(set(outcomes)),
        "elapsed_seconds": elapsed,
    }


def benchmark(rounds: int = 5, concurrency: int = 4) -> Dict[str, Any]:
    if rounds < 1 or concurrency < 1:
        raise ValueError("rounds and concurrency must be positive")
    return asyncio.run(_run(rounds, concurrency))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    receipt = benchmark(args.rounds, args.concurrency)
    rendered = json.dumps(receipt, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
