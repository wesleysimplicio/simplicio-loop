from __future__ import annotations

import json

import pytest

from scripts import benchmark_async_queue


def test_benchmark_reports_bounded_metrics():
    receipt = benchmark_async_queue.benchmark(items=20, capacity=2, repeats=2)
    assert receipt["schema"] == "simplicio.async-queue-benchmark/v1"
    assert receipt["queue"]["max_items"] == 2
    assert receipt["queue"]["items"] == 0
    assert receipt["throughput_per_second"] > 0
    assert receipt["p95_ms"] >= 0
    assert receipt["idle_cpu_percent"] >= 0


def test_capacity_backpressure_is_observable():
    receipt = benchmark_async_queue.benchmark(items=30, capacity=1, repeats=1)
    assert receipt["queue"]["wait_count"] >= 0
    assert receipt["queue"]["accepted"] == 30


def test_invalid_arguments():
    with pytest.raises(ValueError):
        benchmark_async_queue.benchmark(0)
    with pytest.raises(ValueError):
        benchmark_async_queue.benchmark(1, 1, 0)


def test_cli_writes_versioned_artifact(tmp_path, capsys):
    output = tmp_path / "queue.json"
    receipt = benchmark_async_queue.main(["--items", "5", "--capacity", "2", "--repeats", "1", "--output", str(output)])
    assert receipt["output"] == str(output)
    assert json.loads(output.read_text(encoding="utf-8"))["schema"] == "simplicio.async-queue-benchmark/v1"
    assert json.loads(capsys.readouterr().out)["queue"]["max_items"] == 2
