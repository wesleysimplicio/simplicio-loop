from __future__ import annotations

import json
import subprocess

from simplicio_loop.orca_lifecycle import lifecycle_to_orca_status, sync_orca_status


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["orca"], returncode, stdout, stderr)


def test_lifecycle_status_mapping_is_stable():
    assert lifecycle_to_orca_status("PR_OPEN") == "in-review"
    assert lifecycle_to_orca_status("MERGED") == "completed"
    assert lifecycle_to_orca_status("BLOCKED") == "in-progress"


def test_sync_updates_only_active_orca_worktree():
    calls = []

    def runner(args):
        calls.append(args)
        if args[:3] == ["worktree", "current", "--json"]:
            return _completed(stdout=json.dumps({"id": "wt-1"}))
        return _completed(stdout="{}")

    result = sync_orca_status(
        {"run_id": "run-1", "phase": "in_progress"},
        {"lifecycle_state": "PR_OPEN", "message": "PR criada"},
        runner=runner,
    )
    assert result["status"] == "synced"
    assert result["workspace_status"] == "in-review"
    assert calls[-1][0:4] == ["worktree", "set", "--worktree", "active"]
    assert any("PR_OPEN" in value for value in calls[-1])


def test_sync_is_a_typed_noop_outside_orca():
    result = sync_orca_status(
        {"run_id": "run-1"}, {"lifecycle_state": "IN_PROGRESS"},
        runner=lambda args: _completed(returncode=1, stderr="no active worktree"),
    )
    assert result == {"status": "skipped", "reason": "not_in_orca", "detail": "no active worktree"}
