#!/usr/bin/env python3
"""Measured local benchmark for the issue #815 validation controller."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simplicio_loop.progressive_validation import (
    ProgressiveValidator,
    ValidationCommand,
    ValidationLevel,
    ValidationRequest,
    canonical_hash,
    sha256_bytes,
)


class MeasuredExecutor:
    def __call__(self, command):
        started = time.perf_counter_ns()
        sha256_bytes(json.dumps(list(command)).encode())
        return {
            "exit_code": 0,
            "duration_ns": time.perf_counter_ns() - started,
            "stdout_hash": sha256_bytes(b"ok"),
            "stderr_hash": sha256_bytes(b""),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    commands = tuple(
        ValidationCommand(level, ("benchmark", level.value))
        for level in ValidationLevel
    )
    request = ValidationRequest(
        source_hash=sha256_bytes(b"source"),
        tool_hash=sha256_bytes(b"tool"),
        config_hash=sha256_bytes(b"config"),
        commands=commands,
        impact_level=ValidationLevel.FULL,
    )
    cold = []
    warm = []
    with tempfile.TemporaryDirectory() as directory:
        for iteration in range(args.runs):
            cache = Path(directory) / ("%d.json" % iteration)
            started = time.perf_counter_ns()
            ProgressiveValidator(cache, executor=MeasuredExecutor()).run(request)
            cold.append(time.perf_counter_ns() - started)
            started = time.perf_counter_ns()
            ProgressiveValidator(cache, executor=MeasuredExecutor()).run(request)
            warm.append(time.perf_counter_ns() - started)
    payload = {
        "schema": "simplicio.progressive-validation-benchmark/v1",
        "classification": "MEASURED_LOCAL",
        "runs": args.runs,
        "local_llm": False,
        "cold": {
            "median_ns": int(statistics.median(cold)),
            "min_ns": min(cold),
            "max_ns": max(cold),
        },
        "warm": {
            "median_ns": int(statistics.median(warm)),
            "min_ns": min(warm),
            "max_ns": max(warm),
        },
        "limitations": [
            "Controller overhead only; command runtime depends on the repository test suite.",
            "CPU and RSS are null because portable per-call attribution was not available.",
        ],
        "cpu_seconds": None,
        "rss_peak_bytes": None,
    }
    payload["receipt_hash"] = canonical_hash(payload)
    encoded = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
