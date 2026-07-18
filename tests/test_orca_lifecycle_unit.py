from __future__ import annotations

import json
import os
import subprocess

from simplicio_loop.orca_lifecycle import (
    lifecycle_to_orca_status,
    lifecycle_to_orca_canonical_status,
    ORCA_CANONICAL_STATUS_BY_LIFECYCLE,
    sync_orca_status,
)


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["orca"], returncode, stdout, stderr)


def test_lifecycle_status_mapping_is_stable():
    assert lifecycle_to_orca_status("PR_OPEN") == "in-review"
    assert lifecycle_to_orca_status("MERGED") == "completed"
    assert lifecycle_to_orca_status("BLOCKED") == "in-progress"


def test_canonical_orca_mapping_covers_eight_states():
    # Per loop protocol: intake/mapping->Todo, planning->Planning,
    # executing->In progress, validating/watching->Validating,
    # delivering->In review, done->Done, blocked->Blocked,
    # repeated terminal failures->Quarantined.
    assert lifecycle_to_orca_canonical_status("DISCOVERED") == "Todo"
    assert lifecycle_to_orca_canonical_status("CLAIMED") == "Todo"
    assert lifecycle_to_orca_canonical_status("PLANNED") == "Planning"
    assert lifecycle_to_orca_canonical_status("IN_PROGRESS") == "In progress"
    assert lifecycle_to_orca_canonical_status("VERIFYING") == "Validating"
    assert lifecycle_to_orca_canonical_status("WATCHING") == "Validating"
    assert lifecycle_to_orca_canonical_status("PR_OPEN") == "In review"
    assert lifecycle_to_orca_canonical_status("MERGE_READY") == "In review"
    assert lifecycle_to_orca_canonical_status("DELIVERING") == "In review"
    assert lifecycle_to_orca_canonical_status("MERGED") == "Done"
    assert lifecycle_to_orca_canonical_status("CLOSED") == "Done"
    assert lifecycle_to_orca_canonical_status("RELEASED") == "Done"
    assert lifecycle_to_orca_canonical_status("BLOCKED") == "Blocked"
    assert lifecycle_to_orca_canonical_status("QUARANTINED") == "Quarantined"


def test_canonical_mapping_is_total_and_distinct():
    # Every value in the canonical map is one of the 8 allowed Orca statuses.
    allowed = {"Todo", "Planning", "In progress", "Validating", "In review", "Done", "Blocked", "Quarantined"}
    assert set(ORCA_CANONICAL_STATUS_BY_LIFECYCLE.values()) == allowed
    # Unknown lifecycle state falls back to In progress, not a crash.
    assert lifecycle_to_orca_canonical_status("NONEXISTENT") == "In progress"


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


def test_sync_canonical_projection_uses_eight_state_map():
    calls = []

    def runner(args):
        calls.append(args)
        if args[:3] == ["worktree", "current", "--json"]:
            return _completed(stdout=json.dumps({"id": "wt-1"}))
        return _completed(stdout="{}")

    result = sync_orca_status(
        {"run_id": "run-1"},
        {"lifecycle_state": "MERGED", "message": "merged"},
        runner=runner, canonical=True,
    )
    assert result["status"] == "synced"
    assert result["workspace_status"] == "Done"
    assert result["lifecycle_state"] == "MERGED"


def test_sync_canonical_via_env_flag():
    calls = []

    def runner(args):
        calls.append(args)
        if args[:3] == ["worktree", "current", "--json"]:
            return _completed(stdout=json.dumps({"id": "wt-1"}))
        return _completed(stdout="{}")

    env = dict(os.environ)
    env["SIMPLICIO_LOOP_ORCA_CANONICAL"] = "1"
    import unittest.mock as mock
    with mock.patch.dict(os.environ, env, clear=False):
        result = sync_orca_status(
            {"run_id": "run-2"},
            {"lifecycle_state": "BLOCKED", "message": "stuck"},
            runner=runner,
        )
    assert result["status"] == "synced"
    assert result["workspace_status"] == "Blocked"


def test_sync_skips_when_disabled():
    env = dict(os.environ)
    env["SIMPLICIO_LOOP_ORCA_LIFECYCLE_SYNC"] = "off"
    import unittest.mock as mock
    with mock.patch.dict(os.environ, env, clear=False):
        result = sync_orca_status(
            {"run_id": "run-3"}, {"lifecycle_state": "IN_PROGRESS"},
            runner=lambda args: _completed(returncode=1, stderr="no orca"),
        )
    assert result == {"status": "skipped", "reason": "disabled"}


def test_sync_skips_on_invalid_context():
    def runner(args):
        if args[:3] == ["worktree", "current", "--json"]:
            return _completed(stdout="not-json")
        return _completed(stdout="{}")

    result = sync_orca_status(
        {"run_id": "run-4"}, {"lifecycle_state": "IN_PROGRESS"}, runner=runner,
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "invalid_orca_context"


def test_sync_skips_when_no_active_worktree():
    def runner(args):
        if args[:3] == ["worktree", "current", "--json"]:
            return _completed(stdout=json.dumps({"foo": "bar"}))
        return _completed(stdout="{}")

    result = sync_orca_status(
        {"run_id": "run-5"}, {"lifecycle_state": "IN_PROGRESS"}, runner=runner,
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "no_active_worktree"


def test_sync_is_a_typed_noop_outside_orca():
    result = sync_orca_status(
        {"run_id": "run-1"}, {"lifecycle_state": "IN_PROGRESS"},
        runner=lambda args: _completed(returncode=1, stderr="no active worktree"),
    )
    assert result == {"status": "skipped", "reason": "not_in_orca", "detail": "no active worktree"}
