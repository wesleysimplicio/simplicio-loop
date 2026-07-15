"""Unit tests (#284): wiring `github_lifecycle.py`'s comment publish into the
planning gate itself, via `simplicio_loop.planning_gate.publish_planning_receipt()`.

A #284 planning receipt with `ready_for_mutation=True` must be projected onto the
SAME canonical status comment `github_lifecycle.py` already maintains, as `PLANNED`;
a receipt that is not ready must be projected as `BLOCKED` carrying its validator
errors. Local/non-GitHub receipts (no `source` block) are a documented no-op, never
a silent fake publish. No real `gh`/network call is made -- the fake transport here
mirrors `tests/test_github_lifecycle_unit.py`'s own pattern.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from pr_evidence import publish_comment  # noqa: E402  (scripts/ on sys.path for this import)

from simplicio_loop.plan_contract import PLAN_SCHEMA, validate_plan
from simplicio_loop.planning_gate import build_planning_receipt, publish_planning_receipt


def _fake_transport(existing_comment=None, post_id=999):
    state = {"comments": {post_id: existing_comment} if existing_comment else {}}

    def runner(cmd, **kw):
        if cmd[:2] == ["gh", "api"] and len(cmd) >= 3 and "comments" in cmd[2] and "-X" not in cmd and "/comments/" not in cmd[2]:
            listing = [{"id": cid, "body": body} for cid, body in state["comments"].items() if body]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(listing), stderr="")
        if "-X" in cmd and "POST" in cmd:
            input_text = kw.get("input") or "{}"
            body = json.loads(input_text).get("body", "")
            state["comments"][post_id] = body
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"id": post_id}), stderr="")
        if "-X" in cmd and "PATCH" in cmd:
            url = next(part for part in cmd if part.startswith("repos/"))
            comment_id = int(url.rsplit("/", 1)[-1])
            input_text = kw.get("input") or "{}"
            body = json.loads(input_text).get("body", "")
            state["comments"][comment_id] = body
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        if len(cmd) >= 3 and "/comments/" in cmd[2]:
            comment_id = int(cmd[2].rsplit("/", 1)[-1])
            body = state["comments"].get(comment_id)
            if body is None:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not found")
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"id": comment_id, "body": body}), stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected call: %r" % (cmd,))

    return runner, state


def _contract():
    return {"schema": "simplicio.task-contract-collection/v1", "collection_hash": "contract-1",
            "tasks": [{"id": "T1", "scenarios": [{"id": "S1"}], "rules": []}]}


def _plan():
    return {
        "schema": PLAN_SCHEMA,
        "task_contract_hash": "contract-1",
        "mapper_pack_hash": "pack-1",
        "context_pack_hash": "pack-1",
        "repo_state": {"head": "head-1", "tree_hash": "tree-1"},
        "freshness": {"verified": True, "current_state": {"head": "head-1", "tree_hash": "tree-1"}},
        "steps": [{
            "candidate_targets": ["src/app.py"], "to_create": ["src/app.py"], "rule_ids": [],
            "steps": [{"scenario_id": "S1", "plan": {
                "read_paths": ["src/app.py"], "change_paths": ["src/app.py"], "test_commands": ["pytest"],
            }}],
        }],
    }


def _ready_receipt(source_snapshot=None):
    contract, plan = _contract(), _plan()
    validation = validate_plan(plan, contract["tasks"], ".", contract_hash=contract["collection_hash"],
                               current_state={"head": "head-1", "tree_hash": "tree-1"})
    assert validation["valid"], validation["errors"]
    return build_planning_receipt(run_id="run-1", attempt=1, contract=contract, plan=plan,
                                  plan_validation=validation, source_snapshot=source_snapshot)


def test_no_op_when_receipt_has_no_github_source():
    receipt = _ready_receipt(source_snapshot=None)
    assert publish_planning_receipt(receipt, publish_comment_fn=publish_comment) is None


def test_ready_receipt_publishes_planned_state():
    source_snapshot = {"schema": "simplicio.source-snapshot/v1",
                       "source": {"provider": "github", "repo": "acme/widgets", "item_id": "284",
                                  "revision": "r1", "snapshot_hash": "hash-a", "observed_at": "t1"}}
    receipt = _ready_receipt(source_snapshot=source_snapshot)
    assert receipt["ready_for_mutation"] is True

    runner, state = _fake_transport()
    lifecycle_receipt = publish_planning_receipt(
        receipt, publish_comment_fn=publish_comment, runner=runner,
    )
    assert lifecycle_receipt is not None
    assert lifecycle_receipt["state"] == "PLANNED"
    assert lifecycle_receipt["verified"] is True
    assert lifecycle_receipt["repo"] == "acme/widgets"
    assert lifecycle_receipt["issue"] == "284"
    body = next(iter(state["comments"].values()))
    assert "| Estado | PLANNED |" in body


def test_blocked_receipt_publishes_blocked_state_with_errors():
    source_snapshot = {"schema": "simplicio.source-snapshot/v1",
                       "source": {"provider": "github", "repo": "acme/widgets", "item_id": "284",
                                  "revision": "r1", "snapshot_hash": "hash-a", "observed_at": "t1"}}
    contract, plan = _contract(), _plan()
    bad_validation = {"valid": False, "errors": ["task_step_count_mismatch"], "warnings": [], "checked_tasks": 0}
    receipt = build_planning_receipt(run_id="run-1", attempt=1, contract=contract, plan=plan,
                                     plan_validation=bad_validation, source_snapshot=source_snapshot)
    assert receipt["ready_for_mutation"] is False

    runner, state = _fake_transport()
    lifecycle_receipt = publish_planning_receipt(
        receipt, publish_comment_fn=publish_comment, runner=runner,
    )
    assert lifecycle_receipt is not None
    assert lifecycle_receipt["state"] == "BLOCKED"
    body = next(iter(state["comments"].values()))
    assert "BLOCKER: task_step_count_mismatch" in body


def test_second_publish_for_same_issue_updates_same_comment_not_a_new_one():
    source_snapshot = {"schema": "simplicio.source-snapshot/v1",
                       "source": {"provider": "github", "repo": "acme/widgets", "item_id": "284",
                                  "revision": "r1", "snapshot_hash": "hash-a", "observed_at": "t1"}}
    receipt = _ready_receipt(source_snapshot=source_snapshot)
    runner, state = _fake_transport()
    publish_planning_receipt(receipt, publish_comment_fn=publish_comment, runner=runner)
    publish_planning_receipt(receipt, publish_comment_fn=publish_comment, runner=runner)
    assert len(state["comments"]) == 1


def test_no_op_when_source_missing_owner_or_issue():
    source_snapshot = {"schema": "simplicio.source-snapshot/v1",
                       "source": {"provider": "github", "repo": "not-a-full-owner-slash-repo", "item_id": ""}}
    receipt = _ready_receipt(source_snapshot=source_snapshot)
    assert publish_planning_receipt(receipt, publish_comment_fn=publish_comment) is None
