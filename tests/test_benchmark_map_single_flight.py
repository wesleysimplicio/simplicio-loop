import json

import pytest

from scripts import benchmark_map_single_flight


def test_benchmark_proves_one_owner_per_round():
    receipt = benchmark_map_single_flight.benchmark(clients=8, repeats=2)
    assert receipt["schema"] == "simplicio.map-single-flight-benchmark/v1"
    assert receipt["builder_calls"] == receipt["expected_builds"] == 2
    assert receipt["all_clients_shared_snapshot"] is True
    assert receipt["latency_ms_p95"] >= 0
    assert receipt["cpu_seconds"] >= 0


def test_benchmark_rejects_non_positive_inputs():
    with pytest.raises(ValueError):
        benchmark_map_single_flight.benchmark(0, 1)
    with pytest.raises(ValueError):
        benchmark_map_single_flight.benchmark(1, 0)


def test_benchmark_cli_writes_receipt(tmp_path, capsys):
    output = tmp_path / "single-flight.json"
    assert benchmark_map_single_flight.main(["--clients", "2", "--repeats", "1", "--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["builder_calls"] == 1
    assert "simplicio.map-single-flight-benchmark/v1" in capsys.readouterr().out
