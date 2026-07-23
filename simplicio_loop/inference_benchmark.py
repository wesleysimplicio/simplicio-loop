"""Deterministic inference-aware benchmark harness (#679).

The harness is intentionally synthetic and local.  It measures scheduler
effects, not model quality claims, and keeps raw per-task samples alongside
recomputed aggregates so reports can be audited.
"""
from __future__ import annotations

import hashlib
import json
import platform
import sys
from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

SCHEMA = "simplicio.inference-benchmark/v1"
SCENARIOS = ("L0", "L1", "L2", "L3", "L4")


@dataclass(frozen=True)
class SyntheticTask:
    task_id: str
    duration_ticks: int
    cache_key: str
    quality: float = 1.0
    requires_escalation: bool = False

    def __post_init__(self) -> None:
        if not self.task_id or self.duration_ticks < 1 or not self.cache_key:
            raise ValueError("task identity, positive duration and cache key are required")
        if not 0 <= self.quality <= 1:
            raise ValueError("quality must be between 0 and 1")


DEFAULT_WORKLOAD = (
    SyntheticTask("read-1", 2, "repo-prefix", 1.0),
    SyntheticTask("read-2", 2, "repo-prefix", 1.0),
    SyntheticTask("long-1", 5, "long-prefix", 0.92),
    SyntheticTask("duplicate-1", 2, "repo-prefix", 1.0),
    SyntheticTask("hard-1", 4, "hard-prefix", 0.78, True),
)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _manifest(*, scenario: str, seed: int, commit: str, model: str, backend: str) -> Dict[str, Any]:
    return {
        "schema": SCHEMA,
        "scenario": scenario,
        "seed": seed,
        "commit": commit or None,
        "model": model or None,
        "backend": backend or None,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "hardware": {"cpu": platform.processor() or None, "gpu": None, "gpu_reason": "not_observed"},
    }


def aggregate_samples(samples: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Recompute report metrics exclusively from raw samples."""
    if not samples:
        return {"sample_count": 0, "verified_deliveries": 0, "quality": None,
                "quality_reason": "no_samples", "wall_ticks": None, "throughput": None,
                "fairness": None, "fairness_reason": "no_samples", "cache_hits": 0,
                "deduplicated": 0, "escalations": 0, "rss_mb": None, "rss_reason": "not_observed",
                "vram_mb": None, "vram_reason": "not_observed"}
    durations = [int(sample["duration_ticks"]) for sample in samples]
    qualities = [float(sample["quality"]) for sample in samples if sample.get("verified")]
    waits = [int(sample["queue_wait_ticks"]) for sample in samples]
    max_wait = max(waits)
    min_wait = min(waits)
    fairness = 1.0 if max_wait == min_wait else 1.0 - ((max_wait - min_wait) / max(1, max_wait))
    wall = max(int(sample["completed_at"]) for sample in samples)
    return {
        "sample_count": len(samples),
        "verified_deliveries": sum(bool(sample.get("verified")) for sample in samples),
        "quality": round(sum(qualities) / len(qualities), 6) if qualities else None,
        "quality_reason": None if qualities else "no_verified_deliveries",
        "wall_ticks": wall,
        "throughput": round(len(samples) / wall, 6) if wall else None,
        "queue_wait_p50": sorted(waits)[(len(waits) - 1) // 2],
        "queue_wait_p95": sorted(waits)[min(len(waits) - 1, int(len(waits) * 0.95))],
        "fairness": round(fairness, 6),
        "cache_hits": sum(bool(sample.get("cache_hit")) for sample in samples),
        "deduplicated": sum(bool(sample.get("deduplicated")) for sample in samples),
        "escalations": sum(bool(sample.get("escalated")) for sample in samples),
        "rss_mb": None, "rss_reason": "not_observed",
        "vram_mb": None, "vram_reason": "not_observed",
    }


def run_trial(
    scenario: str,
    tasks: Iterable[SyntheticTask] = DEFAULT_WORKLOAD,
    *,
    seed: int = 0,
    capacity: int = 2,
    commit: str = "",
    model: str = "synthetic",
    backend: str = "deterministic",
    existing_samples: Optional[Sequence[Mapping[str, Any]]] = None,
    stop_after: Optional[int] = None,
) -> Dict[str, Any]:
    """Run one repeat; ``existing_samples`` makes interruption/resume idempotent."""
    if scenario not in SCENARIOS or capacity < 1:
        raise ValueError("unsupported scenario or capacity")
    all_tasks = tuple(tasks)
    samples: List[Dict[str, Any]] = [dict(sample) for sample in (existing_samples or ())]
    done = {str(sample.get("task_id")) for sample in samples}
    clock = max((int(sample.get("completed_at", 0)) for sample in samples), default=0)
    cache: set[str] = {str(sample.get("cache_key")) for sample in samples if sample.get("cache_hit")}
    for task in all_tasks:
        if task.task_id in done:
            continue
        queue_wait = clock % max(1, capacity)
        cache_hit = scenario in {"L2", "L3", "L4"} and task.cache_key in cache
        deduplicated = scenario in {"L3", "L4"} and any(sample.get("cache_key") == task.cache_key for sample in samples)
        if deduplicated:
            duration = 0
        elif cache_hit:
            duration = max(1, task.duration_ticks // 2)
        else:
            duration = task.duration_ticks
        escalated = scenario == "L4" and task.requires_escalation
        verified = task.quality >= 0.8 or escalated
        clock += queue_wait + duration
        samples.append({
            "sample_id": _digest({"seed": seed, "scenario": scenario, "task_id": task.task_id})[:16],
            "task_id": task.task_id, "cache_key": task.cache_key,
            "duration_ticks": duration, "queue_wait_ticks": queue_wait,
            "completed_at": clock, "quality": 1.0 if escalated else task.quality,
            "verified": verified, "cache_hit": cache_hit, "deduplicated": deduplicated,
            "escalated": escalated,
        })
        cache.add(task.cache_key)
        if stop_after is not None and len(samples) >= stop_after:
            break
    # A resumed run must never contain duplicate task samples.
    unique: Dict[str, Dict[str, Any]] = {}
    for sample in samples:
        unique.setdefault(str(sample["task_id"]), sample)
    raw = [unique[key] for key in sorted(unique)]
    return {"schema": SCHEMA, "manifest": _manifest(scenario=scenario, seed=seed, commit=commit, model=model, backend=backend),
            "raw_samples": raw, "metrics": aggregate_samples(raw),
            "raw_hash": _digest(raw), "interrupted": len(raw) < len(all_tasks)}


def run_benchmark(*, scenarios: Sequence[str] = SCENARIOS, repeats: int = 1, seed: int = 0,
                  tasks: Iterable[SyntheticTask] = DEFAULT_WORKLOAD, capacity: int = 2,
                  commit: str = "", model: str = "synthetic", backend: str = "deterministic") -> Dict[str, Any]:
    if repeats < 1:
        raise ValueError("repeats must be positive")
    trials = [run_trial(scenario, tasks, seed=seed + index, capacity=capacity, commit=commit, model=model, backend=backend)
              for scenario in scenarios for index in range(repeats)]
    return {"schema": SCHEMA, "scenarios": list(scenarios), "repeats": repeats, "trials": trials,
            "report_hash": _digest(trials)}


__all__ = ["DEFAULT_WORKLOAD", "SCENARIOS", "SCHEMA", "SyntheticTask", "aggregate_samples", "run_benchmark", "run_trial"]
