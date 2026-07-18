from __future__ import annotations

import json

from scripts import benchmark_map_git_worktrees


def test_benchmark_reports_real_build_reduction_across_real_worktrees() -> None:
    receipt = benchmark_map_git_worktrees.benchmark(worktree_count=4, repeats=2)
    assert receipt["schema"] == "simplicio.map-git-worktree-benchmark/v1"
    assert receipt["single_flight"]["logical_builds"] == 2
    assert receipt["single_flight"]["shared_snapshot_every_round"] is True
    assert receipt["naive_full_remap"]["logical_builds"] == 8
    assert receipt["build_reduction_factor"] == 4.0


def test_invalid_arguments() -> None:
    import pytest
    with pytest.raises(ValueError):
        benchmark_map_git_worktrees.benchmark(worktree_count=1)
    with pytest.raises(ValueError):
        benchmark_map_git_worktrees.benchmark(worktree_count=4, repeats=0)


def test_cli_writes_versioned_artifact(tmp_path, capsys) -> None:
    output = tmp_path / "map_git_worktrees.json"
    receipt = benchmark_map_git_worktrees.main(
        ["--worktrees", "3", "--repeats", "1", "--output", str(output)]
    )
    assert receipt == 0
    on_disk = json.loads(output.read_text(encoding="utf-8"))
    assert on_disk["schema"] == "simplicio.map-git-worktree-benchmark/v1"
    assert json.loads(capsys.readouterr().out)["worktree_count"] == 3
