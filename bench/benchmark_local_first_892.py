#!/usr/bin/env python3
"""Offline, model-free benchmark matrix for issue #892."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from bench.prism_benchmark_852 import (  # noqa: E402
    _definitions,
    _legacy,
    _prism,
    _run_repetitions,
    _serial,
)

SCHEMA = "simplicio.local-first-benchmark/v1"


async def benchmark(
    *, repetitions: int = 10, physical_cap: int = 4, delay_seconds: float = 0.0005,
) -> dict[str, Any]:
    if repetitions < 3:
        raise ValueError("repetitions must be >=3")
    if physical_cap < 1 or delay_seconds <= 0:
        raise ValueError("physical_cap must be positive and delay_seconds must be positive")
    loads: dict[str, Any] = {}
    for count in (1, 5, 10, 20, 50):
        slots, tasks = _definitions((count + 9) // 10, conflicted=True, task_count=count)
        ids = [task.task_id for task in tasks]
        cap = min(physical_cap, count)
        loads[str(count)] = {
            "task_count": count,
            "slot_count": len(slots),
            "physical_cap": cap,
            "serial": await _run_repetitions(lambda ids=ids: _serial(ids, delay_seconds), repetitions),
            "physical_capped": await _run_repetitions(
                lambda ids=ids, cap=cap: _legacy(ids, delay_seconds, cap), repetitions,
            ),
            "prism_python": await _run_repetitions(
                lambda slots=slots, tasks=tasks, cap=cap: _prism(slots, tasks, delay_seconds, cap),
                repetitions,
            ),
        }
    return {
        "schema": SCHEMA,
        "measurement": "measured",
        "projection": False,
        "methodology": {
            "warmups": 0,
            "repetitions": repetitions,
            "delay_seconds": delay_seconds,
            "provider_or_model_invoked": False,
            "correctness_before_performance": True,
            "modes": ["serial", "physical_capped", "prism_python"],
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": __import__("os").cpu_count(),
            "provider": None,
            "provider_null_reason": "offline_model_free_benchmark",
        },
        "loads": loads,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--physical-cap", type=int, default=4)
    parser.add_argument("--delay-seconds", type=float, default=0.0005)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    receipt = asyncio.run(benchmark(
        repetitions=args.repetitions,
        physical_cap=args.physical_cap,
        delay_seconds=args.delay_seconds,
    ))
    encoded = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if all(
        load["prism_python"]["correct"] for load in receipt["loads"].values()
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
