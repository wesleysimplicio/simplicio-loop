#!/usr/bin/env python3
"""Measured, model-free Prism benchmark for issue #852."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from simplicio_loop.prism_contracts import (
    PrismExecution,
    SlotSupervisor,
    TaskOwnership,
)
from simplicio_loop.prism_scheduler import (
    BudgetObservation,
    PrismPolicy,
    PrismScheduler,
    ResourceVector,
    ScheduledTask,
)

SCHEMA = "simplicio.prism-benchmark/v1"
SHA_A = "a" * 64
SHA_B = "b" * 64

try:
    import resource
except ImportError:  # pragma: no cover - unavailable on Windows
    resource = None


def _percentile(samples: Sequence[int], percentile: int) -> int | None:
    if not samples:
        return None
    ordered = sorted(samples)
    index = max(0, (len(ordered) * percentile + 99) // 100 - 1)
    return ordered[min(index, len(ordered) - 1)]


def _sha(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _task(task_id: str, slot_id: str, conflict: str | None) -> ScheduledTask:
    ownership = TaskOwnership(
        task_id,
        slot_id,
        1,
        f"agent-{task_id}",
        f"lease-{task_id}",
        1,
        "benchmark-generation",
        ("implementation",),
        ("accepted", "blocked", "cancelled", "failed", "ready", "running"),
    )
    return ScheduledTask(
        task_id,
        slot_id,
        ownership,
        hard_conflicts=(conflict,) if conflict else (),
    )


def _definitions(
    slot_count: int,
    *,
    conflicted: bool,
) -> tuple[list[SlotSupervisor], list[ScheduledTask]]:
    prism = PrismExecution(
        "benchmark-goal",
        "benchmark-root",
        SHA_A,
        SHA_B,
        "benchmark-generation",
        "prism-reducer",
    )
    slots = [
        SlotSupervisor(prism.prism_id, f"supervisor-{index}")
        for index in range(slot_count)
    ]
    tasks: list[ScheduledTask] = []
    for slot_index, slot in enumerate(slots):
        for index in range(10):
            task_id = f"s{slot_index:02d}-t{index:02d}"
            pair = f"s{slot_index:02d}-t{index ^ 1:02d}"
            conflict = pair if conflicted and index < 2 else None
            tasks.append(_task(task_id, slot.slot_id, conflict))
    return slots, tasks


async def _work(
    task_id: str,
    delay_seconds: float,
    invocations: Counter[str],
    active: list[int],
) -> str:
    invocations[task_id] += 1
    active[0] += 1
    active[1] = max(active[1], active[0])
    try:
        await asyncio.sleep(delay_seconds)
    finally:
        active[0] -= 1
    return "accepted"


async def _serial(
    task_ids: Sequence[str],
    delay_seconds: float,
) -> dict[str, Any]:
    invocations: Counter[str] = Counter()
    active = [0, 0]
    started = time.perf_counter_ns()
    states = {}
    for task_id in task_ids:
        states[task_id] = await _work(task_id, delay_seconds, invocations, active)
    return _oracle(task_ids, states, invocations, active[1], time.perf_counter_ns() - started)


async def _legacy(
    task_ids: Sequence[str],
    delay_seconds: float,
    physical_cap: int,
) -> dict[str, Any]:
    invocations: Counter[str] = Counter()
    active = [0, 0]
    semaphore = asyncio.Semaphore(physical_cap)

    async def one(task_id: str) -> tuple[str, str]:
        async with semaphore:
            state = await _work(task_id, delay_seconds, invocations, active)
            return task_id, state

    started = time.perf_counter_ns()
    pairs = await asyncio.gather(*(one(task_id) for task_id in task_ids))
    states = dict(pairs)
    return _oracle(task_ids, states, invocations, active[1], time.perf_counter_ns() - started)


async def _prism(
    slots: Sequence[SlotSupervisor],
    tasks: Sequence[ScheduledTask],
    delay_seconds: float,
    physical_cap: int,
) -> dict[str, Any]:
    scheduler = PrismScheduler(
        PrismPolicy(
            global_worker_limit=physical_cap,
            recovery_reserve=0,
            validation_reserve=0,
        ),
        observation=BudgetObservation(ResourceVector(workers=physical_cap)),
    )
    for slot in slots:
        scheduler.register_slot(slot)
    for task in tasks:
        scheduler.submit(task)
    invocations: Counter[str] = Counter()
    active = [0, 0]

    async def worker(task: ScheduledTask) -> str:
        return await _work(task.task_id, delay_seconds, invocations, active)

    started = time.perf_counter_ns()
    snapshot = await scheduler.execute(worker)
    result = _oracle(
        [task.task_id for task in tasks],
        snapshot["states"],
        invocations,
        active[1],
        time.perf_counter_ns() - started,
    )
    result["scheduler_overlap"] = snapshot["metrics"]["max_temporal_overlap"]
    result["decision_count"] = len(snapshot["decisions"])
    result["snapshot_digest"] = snapshot["digest"]
    return result


def _oracle(
    expected: Sequence[str],
    states: dict[str, str],
    invocations: Counter[str],
    overlap: int,
    wall_ns: int,
) -> dict[str, Any]:
    expected_set = set(expected)
    observed = set(states)
    lost = sorted(expected_set - observed)
    unexpected = sorted(observed - expected_set)
    duplicates = sorted(task_id for task_id, count in invocations.items() if count != 1)
    rejected = sorted(task_id for task_id, state in states.items() if state != "accepted")
    correct = not lost and not unexpected and not duplicates and not rejected
    return {
        "correct": correct,
        "expected_tasks": len(expected),
        "verified_tasks": len(states) - len(rejected),
        "lost_tasks": lost,
        "unexpected_tasks": unexpected,
        "duplicate_or_missing_invocations": duplicates,
        "rejected_tasks": rejected,
        "max_temporal_overlap": overlap,
        "wall_ns": wall_ns,
        "oracle_hash": _sha(
            {
                "expected": sorted(expected),
                "states": dict(sorted(states.items())),
                "invocations": dict(sorted(invocations.items())),
            }
        ),
    }


async def _run_repetitions(
    runner: Callable[[], Awaitable[dict[str, Any]]],
    repetitions: int,
) -> dict[str, Any]:
    raw = [await runner() for _ in range(repetitions)]
    correct = all(row["correct"] for row in raw)
    wall = [row["wall_ns"] for row in raw]
    overlaps = [row["max_temporal_overlap"] for row in raw]
    summary = {
        "measured": True,
        "repetitions": repetitions,
        "correct": correct,
        "wall_ns": {
            "p50": _percentile(wall, 50),
            "p95": _percentile(wall, 95),
            "p99": _percentile(wall, 99),
            "min": min(wall),
            "max": max(wall),
        },
        "max_temporal_overlap": max(overlaps),
        "verified_tasks_per_minute_milli": (
            raw[0]["verified_tasks"] * 60_000_000_000_000 // _percentile(wall, 50)
            if correct and _percentile(wall, 50)
            else None
        ),
        "raw": raw,
    }
    summary["result_hash"] = _sha(summary)
    return summary


def _runtime_probe(runtime_binary: str | None) -> dict[str, Any]:
    resolved = (
        shutil.which(runtime_binary)
        if runtime_binary
        else shutil.which(os.environ.get("SIMPLICIO_RUNTIME_BIN", "simplicio"))
    )
    if not resolved:
        return {
            "available": False,
            "compatible": False,
            "binary": None,
            "version": None,
            "reason_code": "RUNTIME_BINARY_NOT_FOUND",
        }
    try:
        result = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except OSError:
        return {
            "available": False,
            "compatible": False,
            "binary": resolved,
            "version": None,
            "reason_code": "RUNTIME_VERSION_PROBE_FAILED",
        }
    version = (result.stdout or result.stderr).strip() or None
    return {
        "available": result.returncode == 0,
        "compatible": False,
        "binary": resolved,
        "version": version,
        "reason_code": (
            "RUNTIME_PRISM_BENCHMARK_PROTOCOL_UNAVAILABLE"
            if result.returncode == 0
            else "RUNTIME_VERSION_PROBE_FAILED"
        ),
    }


def _git_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except OSError:
        return None
    return result.stdout.strip() or None


async def benchmark(
    *,
    repetitions: int = 10,
    physical_cap: int = 20,
    delay_seconds: float = 0.0005,
    runtime_binary: str | None = None,
) -> dict[str, Any]:
    if repetitions < 10:
        raise ValueError("repetitions must be >=10")
    if not 1 <= physical_cap <= 200:
        raise ValueError("physical_cap must be between 1 and 200")
    if delay_seconds <= 0:
        raise ValueError("delay_seconds must be positive")

    # One warmup uses the same implementation but is excluded from raw samples.
    warm_slots, warm_tasks = _definitions(1, conflicted=True)
    await _prism(warm_slots, warm_tasks, delay_seconds, min(physical_cap, 10))

    runtime = await asyncio.to_thread(_runtime_probe, runtime_binary)
    loads: dict[str, Any] = {}
    for slot_count in (1, 4, 20):
        slots, tasks = _definitions(slot_count, conflicted=True)
        task_ids = [task.task_id for task in tasks]
        cap = min(physical_cap, len(tasks))
        serial = await _run_repetitions(
            lambda ids=task_ids: _serial(ids, delay_seconds),
            repetitions,
        )
        legacy = await _run_repetitions(
            lambda ids=task_ids, active_cap=cap: _legacy(
                ids, delay_seconds, active_cap
            ),
            repetitions,
        )
        prism = await _run_repetitions(
            lambda slot_rows=slots, task_rows=tasks, active_cap=cap: _prism(
                slot_rows,
                task_rows,
                delay_seconds,
                active_cap,
            ),
            repetitions,
        )
        loads[f"{slot_count}x10"] = {
            "logical_tasks": len(tasks),
            "physical_cap": cap,
            "conflicted_tasks": slot_count * 2,
            "S0_serial": serial,
            "S1_legacy": legacy,
            "S2_prism_python": prism,
            "S3_prism_runtime_rust": {
                "measured": False,
                "correct": None,
                "raw": None,
                "null_reason": runtime["reason_code"],
            },
            "S4_python_fallback": {
                "measured": True,
                "correct": prism["correct"],
                "reason_code": runtime["reason_code"],
                "equivalent_result_hash": prism["result_hash"],
            },
        }

    rss = None
    rss_reason = "resource_module_unavailable"
    if resource is not None:
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_reason = None
    commit = await asyncio.to_thread(_git_sha)
    payload = {
        "schema": SCHEMA,
        "measurement": "measured",
        "projection": False,
        "methodology": {
            "warmups": 1,
            "repetitions": repetitions,
            "delay_seconds": delay_seconds,
            "provider_or_model_invoked": False,
            "correctness_before_performance": True,
            "command": (
                "python3 bench/prism_benchmark_852.py --repetitions "
                f"{repetitions} --physical-cap {physical_cap}"
            ),
        },
        "environment": {
            "git_sha": commit or None,
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "os": platform.platform(),
            "machine": platform.machine() or None,
            "cpu_count": os.cpu_count(),
            "rss_max_native_units": rss,
            "rss_null_reason": rss_reason,
            "provider": None,
            "provider_null_reason": "offline_model_free_benchmark",
            "model": None,
            "model_null_reason": "offline_model_free_benchmark",
        },
        "runtime_probe": runtime,
        "loads": loads,
        "fault_evidence": {
            "crash_replay": "tests/test_prism_reducer_recovery_848_849.py",
            "stale_fence": "tests/test_prism_reducer_recovery_848_849.py",
            "corrupt_hbp": "tests/test_prism_reducer_recovery_848_849.py",
            "provider_throttling": "tests/test_prism_budgets_850.py",
            "device_loss": "tests/test_prism_budgets_850.py",
            "same_symbol_collision": "tests/test_prism_reducer_recovery_848_849.py",
        },
        "unobserved_metrics": {
            "cpu_time_per_task": "not_collected_by_stdlib_portable_lane",
            "io_bytes": "portable_process_counter_unavailable",
            "network_bytes": "offline_benchmark",
            "context_tokens": "no_provider_or_model_invoked",
            "conflict_false_positive_rate": "synthetic_conflicts_have_no_mapper_prediction",
            "conflict_false_negative_rate": "synthetic_conflicts_have_no_mapper_prediction",
            "rust_python_modules_loaded": runtime["reason_code"],
        },
    }
    payload["receipt_hash"] = _sha(payload)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--physical-cap", type=int, default=20)
    parser.add_argument("--delay-seconds", type=float, default=0.0005)
    parser.add_argument("--runtime-binary")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    receipt = asyncio.run(
        benchmark(
            repetitions=args.repetitions,
            physical_cap=args.physical_cap,
            delay_seconds=args.delay_seconds,
            runtime_binary=args.runtime_binary,
        )
    )
    encoded = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if all(
        load["S2_prism_python"]["correct"] for load in receipt["loads"].values()
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
