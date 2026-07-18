from __future__ import annotations

import json

import pytest

from scripts import benchmark_hub_governor


def test_benchmark_reports_governed_and_ungoverned_metrics():
    receipt = benchmark_hub_governor.benchmark(tasks=20, workers=4)
    assert receipt["schema"] == "simplicio.hub-governor-benchmark/v1"
    assert receipt["governed"]["throughput_per_second"] > 0
    assert receipt["ungoverned"]["throughput_per_second"] > 0
    assert receipt["governed"]["p95_ms"] >= 0
    assert receipt["ungoverned"]["p95_ms"] >= 0


def test_invalid_arguments():
    with pytest.raises(ValueError):
        benchmark_hub_governor.benchmark(0, 4)
    with pytest.raises(ValueError):
        benchmark_hub_governor.benchmark(4, 0)


def test_cli_writes_versioned_artifact(tmp_path, capsys):
    output = tmp_path / "hub-governor.json"
    receipt = benchmark_hub_governor.main(
        ["--tasks", "10", "--workers", "4", "--output", str(output)]
    )
    assert receipt["output"] == str(output)
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["schema"] == "simplicio.hub-governor-benchmark/v1"
    assert json.loads(capsys.readouterr().out)["tasks"] == 10
