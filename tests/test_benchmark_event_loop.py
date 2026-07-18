from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import benchmark_event_loop


def test_noop_receipt_has_workload_and_resources(monkeypatch):
    monkeypatch.setenv("SIMPLICIO_LOOP_UVLOOP", "0")
    receipt = benchmark_event_loop.benchmark(3, "noop")
    assert receipt["schema"] == "simplicio.event-loop-benchmark/v1"
    assert receipt["workload"] == "noop"
    assert receipt["selection"]["enabled"] is False
    assert receipt["cpu_seconds"] >= 0
    assert receipt["throughput_per_second"] > 0
    assert receipt["rss_source"] in {"resource.getrusage", "tracemalloc"}


def test_gather_workload_and_p95(monkeypatch):
    monkeypatch.setenv("SIMPLICIO_LOOP_UVLOOP", "0")
    receipt = benchmark_event_loop.benchmark(20, "gather")
    assert receipt["workload"] == "gather"
    assert receipt["p95_ms"] >= 0
    assert receipt["peak_rss_mb"] is None or receipt["peak_rss_mb"] >= 0


def test_invalid_arguments():
    with pytest.raises(ValueError):
        benchmark_event_loop.benchmark(0)
    with pytest.raises(ValueError):
        benchmark_event_loop.benchmark(1, "bad")


def test_cli_writes_versioned_receipt(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SIMPLICIO_LOOP_UVLOOP", "0")
    output = tmp_path / "event-loop.json"
    receipt = benchmark_event_loop.main(["--iterations", "3", "--workload", "gather", "--output", str(output)])
    assert receipt["output"] == str(output)
    on_disk = json.loads(output.read_text(encoding="utf-8"))
    assert on_disk["schema"] == "simplicio.event-loop-benchmark/v1"
    assert on_disk["workload"] == "gather"
    assert json.loads(capsys.readouterr().out)["selection"]["name"] == "asyncio"


def test_rollout_flag_can_be_disabled(monkeypatch):
    monkeypatch.setenv("SIMPLICIO_LOOP_UVLOOP", "false")
    receipt = benchmark_event_loop.benchmark(2)
    assert receipt["selection"]["reason"] in {"windows_default", "feature_disabled"}
