#!/usr/bin/env python3
"""Reproducible parse/validation/oracle benchmark for quality-matrix/v2."""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from simplicio_loop.quality_matrix_v2 import evaluate_v2, migrate_v1


def main(argv=None):
    parser = argparse.ArgumentParser(); parser.add_argument("--repeats", type=int, default=1000)
    args = parser.parse_args(argv)
    old = {"schema": "simplicio.quality-matrix/v1", "requirements": {"unit": {"status": "pass"}}, "coverage": {"measured": 90}}
    migrated = migrate_v1(old); encoded = json.dumps(migrated)
    parse, projection, oracle = [], [], []
    for _ in range(args.repeats):
        start = time.perf_counter_ns(); value = json.loads(encoded); parse.append(time.perf_counter_ns() - start)
        start = time.perf_counter_ns(); migrate_v1(old); projection.append(time.perf_counter_ns() - start)
        start = time.perf_counter_ns(); evaluate_v2(value); oracle.append(time.perf_counter_ns() - start)
    result = {"schema": "simplicio.quality-matrix-v2-benchmark/v1", "repeats": args.repeats,
              "median_us": {"parse": statistics.median(parse) / 1000,
                            "projection": statistics.median(projection) / 1000,
                            "oracle": statistics.median(oracle) / 1000}}
    print(json.dumps(result, sort_keys=True, indent=2)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
