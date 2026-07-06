"""Unit tests for `scripts/fan_out.py` (#111) — pure functions, in-process, no subprocess.

Covers the independence-graph partitioner and capacity detector directly against known inputs —
the logic that decides how many workers run and which tasks may run concurrently.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import fan_out  # noqa: E402


def test_task_defaults_have_empty_files_affected():
    t = fan_out.Task(id="1", goal="do a thing")
    assert t.files_affected == []
    assert t.target is None


def test_independence_graph_disjoint_tasks_share_one_group():
    tasks = [
        fan_out.Task(id="1", goal="fix parser", files_affected=["parser.py"]),
        fan_out.Task(id="2", goal="fix ui", files_affected=["ui.py"]),
    ]
    groups = fan_out.build_independence_graph(tasks)
    assert len(groups) == 1
    assert {t.id for t in groups[0]} == {"1", "2"}


def test_independence_graph_overlapping_tasks_split_into_groups():
    tasks = [
        fan_out.Task(id="1", goal="fix parser", files_affected=["parser.py"]),
        fan_out.Task(id="2", goal="also touch parser", files_affected=["parser.py"]),
    ]
    groups = fan_out.build_independence_graph(tasks)
    assert len(groups) == 2
    assert all(len(g) == 1 for g in groups)


def test_independence_graph_is_case_insensitive_on_file_paths():
    tasks = [
        fan_out.Task(id="1", goal="a", files_affected=["Parser.py"]),
        fan_out.Task(id="2", goal="b", files_affected=["parser.PY"]),
    ]
    groups = fan_out.build_independence_graph(tasks)
    assert len(groups) == 2, "case-insensitive collision must still split into separate groups"


def test_independence_graph_no_files_affected_never_collides():
    tasks = [fan_out.Task(id=str(i), goal="no files") for i in range(3)]
    groups = fan_out.build_independence_graph(tasks)
    assert len(groups) == 1
    assert len(groups[0]) == 3


def test_detect_capacity_respects_env_cap():
    old = os.environ.get("FAN_OUT_MAX_WORKERS")
    os.environ["FAN_OUT_MAX_WORKERS"] = "2"
    try:
        cap = fan_out.detect_capacity()
        assert cap["workers_local"] <= 2
        assert "local" in cap["backends"]
    finally:
        _restore_env("FAN_OUT_MAX_WORKERS", old)


def test_detect_capacity_falls_back_to_default_on_bad_env():
    old = os.environ.get("FAN_OUT_MAX_WORKERS")
    os.environ["FAN_OUT_MAX_WORKERS"] = "not-a-number"
    try:
        cap = fan_out.detect_capacity()
        assert cap["workers_local"] == 4
    finally:
        _restore_env("FAN_OUT_MAX_WORKERS", old)


def _restore_env(key, old):
    if old is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = old


def test_run_worker_dry_run_never_touches_git(tmp_path):
    task = fan_out.Task(id="1", goal="dry run me")
    result = fan_out.run_worker(task, str(tmp_path), dry_run=True)
    assert result.success is True
    assert result.task_id == "1"
    assert '"dry_run": true' in result.output


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_fan_out_unit")
