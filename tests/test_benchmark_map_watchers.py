import json

import pytest

from scripts import benchmark_map_watchers


def test_benchmark_proves_one_watcher_per_worktree():
    receipt = benchmark_map_watchers.benchmark(worktrees=3, clients=4, events=2)
    assert receipt["schema"] == "simplicio.map-watcher-benchmark/v1"
    assert receipt["centralized_watchers"] == 3
    assert receipt["naive_watchers"] == 12
    assert receipt["all_events_coalesced"] is True
    assert receipt["coalesced_events"] == receipt["expected_coalesced_events"]


def test_benchmark_rejects_non_positive_inputs():
    with pytest.raises(ValueError):
        benchmark_map_watchers.benchmark(0, 1, 1)


def test_benchmark_cli_writes_receipt(tmp_path, capsys):
    output = tmp_path / "watchers.json"
    assert benchmark_map_watchers.main(["--worktrees", "1", "--clients", "2", "--events", "1", "--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["centralized_watchers"] == 1
    assert "simplicio.map-watcher-benchmark/v1" in capsys.readouterr().out
