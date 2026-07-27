#!/usr/bin/env python3
"""Measure validation-policy selection overhead; not end-to-end delivery time."""

import argparse
import json
import statistics
import time
from typing import Any, Dict, Iterable, Tuple

from simplicio_loop.validation_policy import (
    ValidationCandidate,
    ValidationInputs,
    ValidationPolicy,
)


def _candidates(count: int) -> Tuple[ValidationCandidate, ...]:
    tiers = ("static", "focused", "impacted", "full")
    return tuple(
        ValidationCandidate(name=f"test-{index}", tier=tiers[index % len(tiers)], estimated_ms=1)
        for index in range(count)
    )


def run_case(count: int, repetitions: int) -> Dict[str, Any]:
    candidates = _candidates(count)
    context = tuple(
        (key, value)
        for key, value in (
            ("source_hash", "source"),
            ("test_hash", "tests"),
            ("dependency_hash", "dependencies"),
            ("environment_hash", "environment"),
            ("command_hash", "commands"),
        )
    )
    adaptive = ValidationInputs(phase="edit", candidates=candidates, cache_context=context)
    full = ValidationInputs(phase="pre_promote", candidates=candidates, cache_context=context)
    policy = ValidationPolicy()
    adaptive_ns = []
    full_ns = []
    for _ in range(repetitions):
        start = time.perf_counter_ns()
        adaptive_receipt = policy.decide(adaptive)
        adaptive_ns.append(time.perf_counter_ns() - start)
        start = time.perf_counter_ns()
        full_receipt = policy.decide(full)
        full_ns.append(time.perf_counter_ns() - start)
    adaptive_mean = statistics.mean(adaptive_ns)
    full_mean = statistics.mean(full_ns)
    return {
        "candidate_count": count,
        "repetitions": repetitions,
        "adaptive_selected": len(adaptive_receipt.selected_tests),
        "full_selected": len(full_receipt.selected_tests),
        "adaptive_mean_ns": adaptive_mean,
        "full_mean_ns": full_mean,
        "selection_overhead_ratio": adaptive_mean / full_mean if full_mean else None,
        "measurement_scope": "policy_selection_only",
        "local_llm_started": False,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--counts", default="1,20,100")
    parser.add_argument("--repetitions", type=int, default=10)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.repetitions <= 0:
        parser.error("--repetitions must be positive")
    counts = tuple(int(value) for value in args.counts.split(",") if value.strip())
    if not counts or any(value <= 0 for value in counts):
        parser.error("--counts must contain positive integers")
    print(json.dumps({"schema": "simplicio.validation-policy-benchmark/v1", "cases": [run_case(count, args.repetitions) for count in counts]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
