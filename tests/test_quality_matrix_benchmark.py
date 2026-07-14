"""Benchmark gate test (#278): the quality-matrix gate stays cheap, with a documented
regression ceiling — `scripts/quality_matrix_bench.py` measures a fixed number of repeats
against a committed baseline (`scripts/quality_matrix_bench_baseline.json`)."""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(REPO, "scripts", "quality_matrix_bench.py")
BASELINE = os.path.join(REPO, "scripts", "quality_matrix_bench_baseline.json")


def test_benchmark_baseline_file_is_committed():
    assert os.path.exists(BASELINE), "commit scripts/quality_matrix_bench_baseline.json"
    payload = json.loads(open(BASELINE, encoding="utf-8").read())
    assert payload["median_seconds"] >= 0
    assert payload["repeats"] > 0


def test_benchmark_runs_and_reports_growth_within_documented_ceiling():
    r = subprocess.run([sys.executable, BENCH], capture_output=True, text=True,
                       cwd=REPO, timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stdout + r.stderr
    payload = json.loads(r.stdout)
    assert payload["schema"] == "simplicio.quality-matrix-bench/v1"
    assert payload["ok"] is True
    assert payload["growth"] <= payload["threshold_growth"]
    assert payload["measurement"]["repeats"] > 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_quality_matrix_benchmark")
