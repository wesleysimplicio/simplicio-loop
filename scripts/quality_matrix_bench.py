#!/usr/bin/env python3
"""Deterministic benchmark for the quality-matrix gate (#278).

Measures the wall-clock cost of `evaluate_quality_matrix` over a fixed number of
repeats against a synthetic, all-passing receipt, and fails the run if the
measured cost regresses past a documented multiple of the committed baseline —
the same pattern `scripts/token_budget.py` uses for doc/script size. The gate
that blocks issue closure must stay cheap; this is the guard that proves it.

Usage:
    python3 scripts/quality_matrix_bench.py                  # report + gate vs. baseline
    python3 scripts/quality_matrix_bench.py --update-baseline  # after a deliberate perf change
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from simplicio_loop.quality_matrix import evaluate_quality_matrix, RECEIPT_FILENAME

BASELINE_PATH = os.path.join(HERE, "quality_matrix_bench_baseline.json")
REPEATS = 200
# Deliberately generous: this is a functional regression tripwire (catches an
# accidental O(n^2)/subprocess-in-a-loop mistake), not a tight perf SLO — CI/dev
# machines vary widely in raw single-core speed.
THRESHOLD_GROWTH = 6.0


def _fixture_receipt() -> dict:
    return {
        "schema": "simplicio.quality-matrix/v1",
        "coverage_threshold": 85,
        "requirements": {
            name: {"status": "pass", "proof_ref": f"tests/{name}"}
            for name in ("implementation", "unit", "integration", "system", "regression", "benchmark")
        },
        "coverage": {"measured": 91.2},
    }


def _run_benchmark(repeats: int = REPEATS) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        (run_dir / RECEIPT_FILENAME).write_text(json.dumps(_fixture_receipt()), encoding="utf-8")
        samples = []
        for _ in range(repeats):
            start = time.perf_counter()
            verdict = evaluate_quality_matrix(str(run_dir))
            samples.append(time.perf_counter() - start)
        assert verdict["ready"] is True
    return {
        "repeats": repeats,
        "median_seconds": statistics.median(samples),
        "mean_seconds": statistics.mean(samples),
        "min_seconds": min(samples),
        "max_seconds": max(samples),
    }


def _load_baseline() -> dict | None:
    if not os.path.exists(BASELINE_PATH):
        return None
    try:
        return json.loads(Path(BASELINE_PATH).read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_baseline(measurement: dict) -> None:
    Path(BASELINE_PATH).write_text(json.dumps(measurement, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    measurement = _run_benchmark()
    if "--update-baseline" in argv:
        _write_baseline(measurement)
        print(json.dumps({"updated_baseline": measurement}, ensure_ascii=False, indent=2))
        return 0
    baseline = _load_baseline()
    if baseline is None:
        _write_baseline(measurement)
        print(json.dumps({"baseline_created": measurement}, ensure_ascii=False, indent=2))
        return 0
    baseline_median = float(baseline.get("median_seconds") or 0.0) or 1e-9
    growth = measurement["median_seconds"] / baseline_median
    ok = growth <= THRESHOLD_GROWTH
    report = {
        "schema": "simplicio.quality-matrix-bench/v1",
        "measurement": measurement,
        "baseline_median_seconds": baseline_median,
        "growth": growth,
        "threshold_growth": THRESHOLD_GROWTH,
        "ok": ok,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
