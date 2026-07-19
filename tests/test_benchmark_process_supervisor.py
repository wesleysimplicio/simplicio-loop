from __future__ import annotations

import json

import pytest

from scripts import benchmark_process_supervisor


def test_benchmark_reports_throughput_p95_cpu_rss() -> None:
    receipt = benchmark_process_supervisor.benchmark(items=6, max_concurrency=2, repeats=2)
    assert receipt["schema"] == "simplicio.async-process-supervisor-benchmark/v1"
    assert receipt["total_processes"] == 12
    assert receipt["throughput_per_second"] > 0
    assert receipt["p95_ms"] >= 0
    assert receipt["cpu_percent"] >= 0
    assert receipt["no_leak"] is True


def test_invalid_arguments() -> None:
    with pytest.raises(ValueError):
        benchmark_process_supervisor.benchmark(0)
    with pytest.raises(ValueError):
        benchmark_process_supervisor.benchmark(1, 1, 0)


def test_cli_writes_versioned_artifact(tmp_path, capsys) -> None:
    output = tmp_path / "process_supervisor.json"
    receipt = benchmark_process_supervisor.main(
        ["--items", "4", "--max-concurrency", "2", "--repeats", "1", "--output", str(output)]
    )
    assert receipt["output"] == str(output)
    on_disk = json.loads(output.read_text(encoding="utf-8"))
    assert on_disk["schema"] == "simplicio.async-process-supervisor-benchmark/v1"
    assert json.loads(capsys.readouterr().out)["total_processes"] == 4
