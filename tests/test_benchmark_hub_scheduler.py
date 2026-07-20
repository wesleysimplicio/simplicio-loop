from __future__ import annotations

import json

import pytest

from scripts import benchmark_hub_scheduler


def test_benchmark_reports_fairness_throughput_and_rss():
    receipt = benchmark_hub_scheduler.benchmark(heavy_jobs=50, light_jobs=20)
    assert receipt["schema"] == "simplicio.hub-scheduler-benchmark/v1"
    assert receipt["served"]["heavy"] == 50
    assert receipt["served"]["light"] == 20
    assert receipt["throughput_per_second"] > 0
    assert receipt["dispatch_p95_ms"] >= 0
    assert 0.0 < receipt["jains_fairness_index"] <= 1.0
    assert receipt["rss_source"] in ("resource.getrusage", "unavailable")


def test_invalid_arguments():
    with pytest.raises(ValueError):
        benchmark_hub_scheduler.benchmark(0, 4)
    with pytest.raises(ValueError):
        benchmark_hub_scheduler.benchmark(4, 0)


def test_cli_writes_versioned_artifact(tmp_path, capsys):
    output = tmp_path / "hub-scheduler.json"
    receipt = benchmark_hub_scheduler.main(
        ["--heavy-jobs", "10", "--light-jobs", "5", "--output", str(output)]
    )
    assert receipt["output"] == str(output)
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["schema"] == "simplicio.hub-scheduler-benchmark/v1"
    assert json.loads(capsys.readouterr().out)["heavy_jobs"] == 10
