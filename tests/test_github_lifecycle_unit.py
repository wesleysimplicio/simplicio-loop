"""Unit + fake-transport integration tests for the #285 GitHub lifecycle adapter.

`simplicio_loop/github_lifecycle.py` builds on top of the #295 idempotent
create-or-update comment primitive (`scripts/pr_evidence.py::publish_comment`)
rather than re-implementing it: the state machine, renderer, and the
publish-then-re-query confirmation are what's new here. No real `gh`/network
call is ever made in this file -- `publish_comment` is imported from
`scripts/pr_evidence.py` and driven with a fake `runner` callable, exactly like
`scripts/pr_evidence.py`'s own selftest does.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from pr_evidence import publish_comment  # noqa: E402  (scripts/ on sys.path for this import)

from simplicio_loop.github_lifecycle import (
    LIFECYCLE_COMMENT_MARKER,
    LIFECYCLE_STATES,
    content_hash,
    operation_id,
    publish_lifecycle_state,
    render_lifecycle_comment,
    validate_transition,
)


# --- state machine ------------------------------------------------------------------


def test_all_documented_states_present():
    assert set(LIFECYCLE_STATES) == {
        "DISCOVERED", "CLAIMED", "PLANNED", "IN_PROGRESS", "VERIFYING", "BLOCKED",
        "PAUSED_NETWORK", "AWAITING_DECISION", "PR_OPEN", "MERGE_READY", "MERGED",
        "CLOSING", "CLOSE_PENDING_RECONCILIATION", "CLOSED", "RELEASED",
    }


def test_happy_path_transitions_are_valid():
    path = ["DISCOVERED", "CLAIMED", "PLANNED", "IN_PROGRESS", "VERIFYING",
            "PR_OPEN", "MERGE_READY", "MERGED", "CLOSING", "CLOSED", "RELEASED"]
    for a, b in zip(path, path[1:]):
        verdict = validate_transition(a, b)
        assert verdict["ok"] is True, (a, b, verdict)


def test_duplicate_event_is_noop_idempotent():
    verdict = validate_transition("PLANNED", "PLANNED")
    assert verdict["ok"] is True
    assert verdict["reason_code"] == "duplicate_noop"


def test_unknown_states_rejected():
    assert validate_transition("NOT_A_STATE", "CLAIMED")["ok"] is False
    assert validate_transition("CLAIMED", "NOT_A_STATE")["ok"] is False


def test_invalid_forward_jump_rejected_without_reason_code():
    verdict = validate_transition("CLAIMED", "MERGED")
    assert verdict["ok"] is False
    assert verdict["reason_code"] == "transition_invalid"


def test_regression_requires_an_authorized_reason_code():
    unauthorized = validate_transition("PR_OPEN", "IN_PROGRESS", reason_code="I_FELT_LIKE_IT")
    assert unauthorized["ok"] is False

    authorized = validate_transition("PR_OPEN", "IN_PROGRESS", reason_code="SOURCE_CHANGED")
    assert authorized["ok"] is True
    assert authorized["reason_code"] == "SOURCE_CHANGED"


def test_close_pending_reconciliation_only_resolves_to_closed():
    assert validate_transition("CLOSE_PENDING_RECONCILIATION", "CLOSED")["ok"] is True
    assert validate_transition("CLOSE_PENDING_RECONCILIATION", "MERGED")["ok"] is False


# --- renderer -----------------------------------------------------------------------


def test_render_includes_marker_and_state_table():
    body = render_lifecycle_comment(state="PLANNED", run_id="run-1", attempt_id="issue-123-1",
                                    agent_id="agent-a", lease_id="lease-1", fencing_token="7")
    assert LIFECYCLE_COMMENT_MARKER in body
    assert "| Estado | PLANNED |" in body
    assert "run-1 / issue-123-1" in body
    assert "agent-a" in body
    assert "lease-1 / 7" in body


def test_render_acceptance_criteria_checklist():
    body = render_lifecycle_comment(
        state="PLANNED", run_id="r", attempt_id="a",
        acceptance_criteria=[{"id": "AC-001", "text": "renders the button", "done": False},
                             {"id": "AC-002", "text": "redirects to IdP", "done": True}],
    )
    assert "- [ ] **AC-001** renders the button" in body
    assert "- [x] **AC-002** redirects to IdP" in body


def test_render_redacts_secrets():
    body = render_lifecycle_comment(state="IN_PROGRESS", run_id="r", attempt_id="a",
                                    progress="token: ghp_abcdefghijklmnopqrstuvwxyz1234 leaked in logs")
    assert "ghp_abcdefghijklmnopqrstuvwxyz1234" not in body
    assert "[REDACTED" in body


def test_render_rejects_unknown_state():
    try:
        render_lifecycle_comment(state="NOPE", run_id="r", attempt_id="a")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unknown state")


def test_render_is_deterministic_for_same_inputs():
    kwargs = dict(state="PLANNED", run_id="r", attempt_id="a", goal="ship SSO",
                  acceptance_criteria=[{"id": "AC-1", "text": "x", "done": False}])
    assert render_lifecycle_comment(**kwargs) == render_lifecycle_comment(**kwargs)


# --- operation_id ---------------------------------------------------------------------


def test_operation_id_deterministic_and_sensitive_to_every_field():
    base = dict(provider="github", repo="acme/widgets", issue="12", run_id="r1", attempt_id="a1",
                fencing_token="7", lifecycle_revision=1, operation_kind="claim")
    assert operation_id(**base) == operation_id(**base)
    for field, new_value in (("issue", "13"), ("run_id", "r2"), ("attempt_id", "a2"),
                              ("fencing_token", "8"), ("lifecycle_revision", 2),
                              ("operation_kind", "close")):
        variant = dict(base, **{field: new_value})
        assert operation_id(**variant) != operation_id(**base), field


# --- publish_lifecycle_state (fake transport, no real gh/network) --------------------


def _fake_transport(existing_comment=None, post_id=999):
    """Build a fake `runner` that services find/create/update/re-query calls."""
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


def test_first_claim_creates_one_comment_and_verifies():
    runner, state = _fake_transport()
    receipt = publish_lifecycle_state(
        owner="acme", repo="widgets", issue="12", state="CLAIMED",
        run_id="run-1", attempt_id="issue-12-1", fencing_token="1",
        publish_comment_fn=publish_comment, runner=runner,
    )
    assert receipt["action"] == "created"
    assert receipt["verified"] is True
    assert receipt["outcome"] == "created"
    assert len(state["comments"]) == 1


def test_second_identical_publish_updates_the_same_comment_not_a_new_one():
    runner, state = _fake_transport()
    r1 = publish_lifecycle_state(owner="acme", repo="widgets", issue="12", state="CLAIMED",
                                 run_id="run-1", attempt_id="issue-12-1",
                                 publish_comment_fn=publish_comment, runner=runner)
    r2 = publish_lifecycle_state(owner="acme", repo="widgets", issue="12", state="PLANNED",
                                 run_id="run-1", attempt_id="issue-12-1",
                                 publish_comment_fn=publish_comment, runner=runner)
    assert r1["comment_id"] == r2["comment_id"]
    assert r2["action"] == "updated"
    assert r2["verified"] is True
    assert len(state["comments"]) == 1  # never appended a second comment


def test_progress_evidence_and_close_all_update_the_same_comment_id():
    runner, state = _fake_transport()
    ids = []
    for state_name in ("CLAIMED", "PLANNED", "IN_PROGRESS", "VERIFYING", "PR_OPEN",
                       "MERGE_READY", "MERGED", "CLOSING", "CLOSED"):
        receipt = publish_lifecycle_state(owner="acme", repo="widgets", issue="12", state=state_name,
                                          run_id="run-1", attempt_id="issue-12-1",
                                          publish_comment_fn=publish_comment, runner=runner)
        assert receipt["verified"] is True, state_name
        ids.append(receipt["comment_id"])
    assert len(set(ids)) == 1
    assert len(state["comments"]) == 1


def test_re_query_mismatch_is_reported_as_unverified_not_a_fake_pass():
    # Simulates a write that "succeeds" per gh's exit code but whose re-query
    # observes a different body than what was just sent (e.g. another actor
    # raced a write in between) -- must be reported as verified=False/blocked,
    # never silently accepted.
    def flaky_runner(cmd, **kw):
        if cmd[:2] == ["gh", "api"] and len(cmd) >= 3 and "comments" in cmd[2] and "-X" not in cmd and "/comments/" not in cmd[2]:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        if "-X" in cmd and "POST" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"id": 777}), stderr="")
        if "/comments/" in cmd[2]:
            # observed body deliberately differs from whatever was published
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"id": 777, "body": "raced-by-someone-else"}), stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected")

    receipt = publish_lifecycle_state(owner="acme", repo="widgets", issue="12", state="CLAIMED",
                                      run_id="run-1", attempt_id="issue-12-1",
                                      publish_comment_fn=publish_comment, runner=flaky_runner)
    assert receipt["verified"] is False
    assert receipt["outcome"] == "blocked"


def test_receipt_carries_full_identity_and_hashes():
    runner, _ = _fake_transport()
    receipt = publish_lifecycle_state(owner="acme", repo="widgets", issue="12", state="CLAIMED",
                                      run_id="run-1", attempt_id="issue-12-1", fencing_token="9",
                                      lifecycle_revision=3,
                                      publish_comment_fn=publish_comment, runner=runner)
    assert receipt["schema"] == "simplicio.github-lifecycle-receipt/v1"
    assert receipt["repo"] == "acme/widgets"
    assert receipt["issue"] == "12"
    assert receipt["fencing_token"] == "9"
    assert receipt["expected_body_hash"] == receipt["observed_body_hash"]
    expected_body = render_lifecycle_comment(state="CLAIMED", run_id="run-1", attempt_id="issue-12-1",
                                             fencing_token="9")
    assert receipt["expected_body_hash"] == content_hash(expected_body)
