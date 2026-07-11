import hashlib
import json
import subprocess
import sys
from pathlib import Path

from scripts.independent_watcher import PLAN_SCHEMA, git_observation, verify


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          stdin=subprocess.DEVNULL, text=True).stdout.strip()


def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "probe.py").write_text("print('ok')\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=watcher-test", "-c", "user.email=watcher@example.invalid",
         "commit", "-qm", "fixture")
    return root


def _plan(root, code="from pathlib import Path; raise SystemExit(0 if Path('probe.py').exists() else 2)"):
    observed = git_observation(root)
    return {
        "schema": PLAN_SCHEMA,
        "challenge": "challenge-1",
        "run_id": "run-1",
        "commit_sha": observed["commit_sha"],
        "diff_hash": observed["diff_hash"],
        "criteria": [{"id": "AC1", "argv": [sys.executable, "-c", code], "expected_exit_code": 0}],
    }


def test_independent_watcher_runs_against_clean_detached_snapshot(tmp_path):
    root = _repo(tmp_path)
    receipt = verify(str(root), _plan(root))
    assert receipt["match"] is True
    assert receipt["status"] == "MEASURED"
    assert receipt["producer"]["snapshot"] is True
    assert receipt["criteria_results"][0]["status"] == "MEASURED"
    criterion = receipt["criteria_results"][0]
    assert criterion["process_isolated"] is True
    assert criterion["runner_pid"] != criterion["watcher_pid"]
    assert receipt["tool_versions"]["python"]
    assert receipt["receipt_hash"]


def test_mutated_behavior_is_rejected_even_when_plan_has_expected_success(tmp_path):
    root = _repo(tmp_path)
    receipt = verify(str(root), _plan(root, "raise SystemExit(7)"))
    assert receipt["match"] is False
    assert receipt["criteria_results"][0]["status"] == "UNVERIFIED"


def test_stale_commit_and_dirty_tree_fail_closed(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(root)
    plan["commit_sha"] = "0" * 40
    stale = verify(str(root), plan)
    assert stale["match"] is False
    assert "commit_mismatch" in stale["errors"]

    (root / "probe.py").write_text("print('changed')\n", encoding="utf-8")
    dirty = verify(str(root), _plan(root))
    assert dirty["match"] is False
    assert "dirty_tree_requires_committed_snapshot" in dirty["errors"]
