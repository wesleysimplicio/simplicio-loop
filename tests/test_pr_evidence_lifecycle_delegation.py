"""#285 remaining gap: `pr_evidence.py`'s evidence-comment publish path must delegate to the
canonical GitHub lifecycle comment (`simplicio_loop.github_lifecycle.publish_lifecycle_state`)
instead of posting a second, separate comment under its own `PR_EVIDENCE_COMMENT_MARKER`.

Before this change, `cmd_comment --publish` called `publish_comment(...)` directly with the
default marker, which opens/updates a SEPARATE comment from the one `claim`/`PLANNED`
(`planning_gate.publish_planning_receipt`) already maintain on the same issue — a direct
violation of #285's "Um comentário canônico: claim, planejamento, progresso, evidência e
fechamento atualizam o mesmo comment ID."

These tests exercise `publish_evidence_via_lifecycle` (the new delegation helper) in-process with
a fake `gh` transport (no real network/`gh` call), mirroring
`tests/test_planning_gate_github_publish.py`'s own fake-transport pattern so both PLANNED and
VERIFYING/PR_OPEN updates are proven to land on the exact same comment id.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pr_evidence  # noqa: E402  (scripts/ on sys.path for this import)

from simplicio_loop.github_lifecycle import LIFECYCLE_COMMENT_MARKER  # noqa: E402
from simplicio_loop.planning_gate import publish_planning_receipt, build_planning_receipt  # noqa: E402
from simplicio_loop.plan_contract import PLAN_SCHEMA, validate_plan  # noqa: E402


def _fake_transport(existing_comment=None, post_id=999):
    state = {"comments": {post_id: existing_comment} if existing_comment else {}}

    def runner(cmd, **kw):
        if cmd[:2] == ["gh", "api"] and len(cmd) >= 3 and "comments" in cmd[2] and \
                "-X" not in cmd and "/comments/" not in cmd[2]:
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
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"id": comment_id, "body": body}),
                                               stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected call: %r" % (cmd,))

    return runner, state


def test_publish_evidence_defaults_to_verifying_without_a_pr():
    runner, state = _fake_transport()
    receipt = pr_evidence.publish_evidence_via_lifecycle(
        "acme", "widgets", "12", "Verification: 2/2 acceptance criteria met.", runner=runner)
    assert receipt["state"] == "VERIFYING"
    assert receipt["verified"] is True
    body = next(iter(state["comments"].values()))
    assert LIFECYCLE_COMMENT_MARKER in body
    assert pr_evidence.PR_EVIDENCE_COMMENT_MARKER not in body
    assert "Verification: 2/2 acceptance criteria met." in body


def test_publish_evidence_uses_pr_open_state_when_pr_given():
    runner, state = _fake_transport()
    receipt = pr_evidence.publish_evidence_via_lifecycle(
        "acme", "widgets", "12", "PR: #34\n\nVerification: 1/1.", pr="34", runner=runner)
    assert receipt["state"] == "PR_OPEN"
    body = next(iter(state["comments"].values()))
    assert "PR #34" in body


def test_publish_evidence_reuses_the_same_comment_as_the_planning_receipt():
    """claim/PLANNED (`publish_planning_receipt`) and the evidence comment
    (`publish_evidence_via_lifecycle`) must land on the SAME comment id for the same issue --
    the whole point of #285's "um único comentário de status editável por issue"."""
    runner, state = _fake_transport()

    contract = {"schema": "simplicio.task-contract-collection/v1", "collection_hash": "contract-1",
               "tasks": [{"id": "T1", "scenarios": [{"id": "S1"}], "rules": []}]}
    plan = {
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
    validation = validate_plan(plan, contract["tasks"], ".", contract_hash=contract["collection_hash"],
                               current_state={"head": "head-1", "tree_hash": "tree-1"})
    assert validation["valid"], validation["errors"]
    source_snapshot = {"schema": "simplicio.source-snapshot/v1",
                       "source": {"provider": "github", "repo": "acme/widgets", "item_id": "12",
                                  "revision": "r1", "snapshot_hash": "hash-a", "observed_at": "t1"}}
    planning_receipt = build_planning_receipt(run_id="run-1", attempt=1, contract=contract, plan=plan,
                                              plan_validation=validation, source_snapshot=source_snapshot)
    planned_receipt = publish_planning_receipt(
        planning_receipt, publish_comment_fn=pr_evidence.publish_comment, runner=runner)
    assert planned_receipt["state"] == "PLANNED"

    evidence_receipt = pr_evidence.publish_evidence_via_lifecycle(
        "acme", "widgets", "12", "Verification: 2/2 acceptance criteria met.",
        pr="34", run_id="run-1", attempt_id="1", runner=runner)
    assert evidence_receipt["state"] == "PR_OPEN"
    assert evidence_receipt["comment_id"] == planned_receipt["comment_id"]
    assert len(state["comments"]) == 1  # exactly one comment total on the issue


def test_publish_evidence_propagates_transport_failure_fail_closed():
    def failing_runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 403: Forbidden")

    try:
        pr_evidence.publish_evidence_via_lifecycle(
            "acme", "widgets", "12", "body", runner=failing_runner)
        assert False, "expected PublishError"
    except pr_evidence.PublishError:
        pass


if __name__ == "__main__":
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_pr_evidence_lifecycle_delegation")
