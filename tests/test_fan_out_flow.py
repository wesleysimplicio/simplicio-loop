"""Flow test for `scripts/fan_out.py` (#111) — drives the full CLI end-to-end via subprocess:

argv parsing → task loading → capacity detection → independence-graph partition → parallel
worker dispatch → aggregated JSON report. Exercises the whole `main()` flow the way a real
caller would, not just the pure functions (see `test_fan_out_unit.py` for those).
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAN_OUT = os.path.join(REPO, "scripts", "fan_out.py")


def _run(args, cwd, env=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run([sys.executable, FAN_OUT] + args, capture_output=True, text=True,
                          cwd=cwd, env=full_env, timeout=30)


def _write_tasks(tmp_path, tasks):
    p = tmp_path / "tasks.json"
    p.write_text(json.dumps(tasks), encoding="utf-8")
    return str(p)


def test_flow_no_tasks_reports_serial(tmp_path):
    tasks_path = _write_tasks(tmp_path, [])
    r = _run(["--tasks", tasks_path], cwd=str(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    report = json.loads(r.stdout)
    assert report["verdict"].startswith("SERIAL")


def test_flow_single_task_serial_no_extra_capacity(tmp_path):
    tasks_path = _write_tasks(tmp_path, [{"id": "1", "goal": "solo task"}])
    r = _run(["--tasks", tasks_path, "--max-workers", "1"], cwd=str(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    report = json.loads(r.stdout)
    assert report["verdict"].startswith("SERIAL")


def test_flow_multiple_independent_tasks_fan_out_dry_run(tmp_path):
    tasks = [
        {"id": "1", "goal": "fix parser", "files_affected": ["parser.py"]},
        {"id": "2", "goal": "fix ui", "files_affected": ["ui.py"]},
        {"id": "3", "goal": "fix docs", "files_affected": ["README.md"]},
    ]
    tasks_path = _write_tasks(tmp_path, tasks)
    r = _run(["--tasks", tasks_path, "--max-workers", "3", "--dry-run"], cwd=str(tmp_path),
              env={"FAN_OUT_MAX_WORKERS": "3"})
    assert r.returncode == 0, r.stdout + r.stderr
    report = json.loads(r.stdout)
    assert report["verdict"] == "FAN_OUT"
    assert report["total_tasks"] == 3
    assert len(report["workers"]) == 3
    assert all(w["success"] for w in report["workers"])
    assert "savings" in report


def test_flow_missing_tasks_file_errors_cleanly(tmp_path):
    r = _run(["--tasks", str(tmp_path / "nope.json")], cwd=str(tmp_path))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "Error loading tasks" in r.stderr


def test_flow_no_args_prints_usage(tmp_path):
    r = _run([], cwd=str(tmp_path))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "Usage" in r.stdout


def test_flow_selftest_verb():
    r = subprocess.run([sys.executable, FAN_OUT, "selftest"], capture_output=True, text=True,
                       cwd=REPO, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ALL PASS" in r.stdout, r.stdout


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_fan_out_flow")
