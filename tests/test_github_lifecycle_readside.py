"""Tests for the #285 read-side verbs (`list_ready`/`get_details`/`requery`/`reconcile`),
the outbox, lease-gated publish, and the fail-closed `close_source_issue` wiring.

Same discipline as `tests/test_github_lifecycle_unit.py`: no real `gh`/network call is
ever made -- every test drives a fake `runner` callable.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from pr_evidence import publish_comment  # noqa: E402

from simplicio_loop.github_lifecycle import (  # noqa: E402
    GitHubTransportError,
    LIFECYCLE_COMMENT_MARKER,
    close_source_issue,
    get_details,
    list_pending_operations,
    list_ready,
    mark_operation_done,
    publish_lifecycle_state,
    record_pending_operation,
    reconcile,
    requery,
    verify_issue_state,
)


def _issue_payload(number=12, state="open", title="fix the thing", labels=None,
                   assignees=None, milestone=None, body="do the thing", pull_request=False):
    payload = {
        "number": number, "title": title, "state": state, "state_reason": None,
        "body": body, "html_url": "https://github.com/acme/widgets/issues/%d" % number,
        "labels": [{"name": l} for l in (labels or [])],
        "assignees": [{"login": a} for a in (assignees or [])],
        "milestone": {"title": milestone} if milestone else None,
        "user": {"login": "author-a"},
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z",
    }
    if pull_request:
        payload["pull_request"] = {"url": "https://api.github.com/x"}
    return payload


def _fake_gh(*, issue=None, comments=None, list_items=None, close_ok=True, closed_after=True):
    """A single fake runner covering issue view / comments list / close / (re)view-after-close."""
    comments = list(comments or [])
    state = {"closed": False}

    def runner(cmd, **kw):
        joined = cmd
        if joined[:2] == ["gh", "issue"] and "close" in joined:
            state["closed"] = True
            return subprocess.CompletedProcess(cmd, 0 if close_ok else 1, stdout="",
                                               stderr="" if close_ok else "HTTP 403: Forbidden")
        if joined[:2] == ["gh", "api"] and len(joined) >= 3:
            url = joined[2]
            if "/issues/" in url and "/comments" not in url and "?" not in url.split("/issues/")[0]:
                # single-issue view: repos/{owner}/{repo}/issues/{n}
                data = dict(issue or _issue_payload())
                if state["closed"] and closed_after:
                    data["state"] = "closed"
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(data), stderr="")
            if "/comments" in url and "/issues/comments/" not in url:
                page = 1
                for part in url.split("&"):
                    if part.startswith("page="):
                        page = int(part.split("=", 1)[1])
                batch = comments if page == 1 else []
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(batch), stderr="")
            if "/issues/comments/" in url:
                cid = int(url.rsplit("/", 1)[-1])
                match = next((c for c in comments if c.get("id") == cid), None)
                if match is None:
                    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not found")
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(match), stderr="")
            if "/issues?" in url:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(list_items or []), stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected call: %r" % (cmd,))

    return runner, state


# --- list_ready -----------------------------------------------------------------------


def test_list_ready_excludes_pull_requests_and_sorts_deterministically():
    items = [_issue_payload(number=5), _issue_payload(number=2, pull_request=True),
             _issue_payload(number=9)]
    runner, _ = _fake_gh(list_items=items)
    result = list_ready("acme", "widgets", runner=runner)
    numbers = [item["number"] for item in result["items"]]
    assert numbers == [5, 9]  # PR (2) excluded; ascending order
    assert result["count"] == 2
    assert result["schema"] == "simplicio.github-list-ready/v1"


def test_list_ready_query_is_reflected_in_receipt():
    runner, _ = _fake_gh(list_items=[])
    result = list_ready("acme", "widgets", state="open", labels=["bug"], assignee="dev-a", runner=runner)
    assert result["query"] == {"state": "open", "labels": ["bug"], "assignee": "dev-a", "milestone": ""}


# --- get_details ------------------------------------------------------------------------


def test_get_details_separates_canonical_from_human_comments():
    human = {"id": 1, "user": {"login": "someone"}, "body": "looks good", "created_at": "t"}
    canonical = {"id": 2, "user": {"login": "loop-bot"},
                "body": "status\n" + LIFECYCLE_COMMENT_MARKER, "created_at": "t2"}
    runner, _ = _fake_gh(comments=[human, canonical])
    snapshot = get_details("acme", "widgets", "12", runner=runner)
    assert len(snapshot["human_comments"]) == 1
    assert snapshot["human_comments"][0]["body"] == "looks good"
    assert snapshot["canonical_comment"]["id"] == 2
    assert snapshot["source_revision"]


def test_get_details_rejects_pull_requests():
    runner, _ = _fake_gh(issue=_issue_payload(pull_request=True))
    try:
        get_details("acme", "widgets", "12", runner=runner)
    except GitHubTransportError as exc:
        assert exc.reason_code == "SOURCE_NOT_FOUND"
    else:
        raise AssertionError("expected GitHubTransportError")


def test_get_details_raises_typed_error_on_gh_failure():
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 403: Forbidden")

    try:
        get_details("acme", "widgets", "12", runner=runner)
    except GitHubTransportError as exc:
        assert exc.reason_code == "PERMISSION_DENIED"
    else:
        raise AssertionError("expected GitHubTransportError")


def test_get_details_raises_typed_error_on_non_json_issue_body():
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="not json", stderr="")

    try:
        get_details("acme", "widgets", "12", runner=runner)
    except GitHubTransportError as exc:
        assert exc.reason_code == "NETWORK_UNAVAILABLE"
    else:
        raise AssertionError("expected GitHubTransportError")


def test_get_details_source_revision_ignores_the_canonical_comment_but_not_human_edits():
    base_comments = []
    runner1, _ = _fake_gh(comments=base_comments)
    snap1 = get_details("acme", "widgets", "12", runner=runner1)

    # adding the Loop's OWN comment must not change source_revision materially in a way
    # that looks like a human edit -- but here we simply confirm two different snapshots
    # (no canonical comment vs. one canonical comment) still hash identically because the
    # canonical comment is excluded from `authoritative`.
    canonical = {"id": 9, "user": {"login": "loop-bot"}, "body": "x\n" + LIFECYCLE_COMMENT_MARKER, "created_at": "t"}
    runner2, _ = _fake_gh(comments=[canonical])
    snap2 = get_details("acme", "widgets", "12", runner=runner2)
    assert snap1["source_revision"] == snap2["source_revision"]

    human = {"id": 3, "user": {"login": "someone"}, "body": "material change request", "created_at": "t"}
    runner3, _ = _fake_gh(comments=[human])
    snap3 = get_details("acme", "widgets", "12", runner=runner3)
    assert snap3["source_revision"] != snap1["source_revision"]


# --- requery --------------------------------------------------------------------------


def test_requery_reports_comment_hash_for_a_known_id():
    canonical = {"id": 42, "user": {"login": "loop-bot"}, "body": "hello\n" + LIFECYCLE_COMMENT_MARKER, "created_at": "t"}
    runner, _ = _fake_gh(comments=[canonical])
    snapshot = requery("acme", "widgets", "12", comment_id=42, runner=runner)
    assert snapshot["requeried_comment"]["found"] is True
    assert snapshot["requeried_comment"]["id"] == 42
    assert "requeried_at" in snapshot


def test_requery_reports_missing_comment():
    runner, _ = _fake_gh(comments=[])
    snapshot = requery("acme", "widgets", "12", comment_id=999, runner=runner)
    assert snapshot["requeried_comment"]["found"] is False


# --- outbox + reconcile -----------------------------------------------------------------


def test_outbox_round_trip(tmp_path):
    op_id = "op-1"
    record_pending_operation(tmp_path, op_id, {"issue": "12"})
    pending = list_pending_operations(tmp_path)
    assert len(pending) == 1 and pending[0]["operation_id"] == op_id

    mark_operation_done(tmp_path, op_id, {"verified": True})
    assert list_pending_operations(tmp_path) == []


def test_reconcile_recovers_a_confirmed_write_without_a_second_comment(tmp_path):
    canonical_body = "state\n" + LIFECYCLE_COMMENT_MARKER
    canonical = {"id": 7, "user": {"login": "loop-bot"}, "body": canonical_body, "created_at": "t"}
    runner, _ = _fake_gh(comments=[canonical])
    from simplicio_loop.github_lifecycle import content_hash
    expected_hash = content_hash(canonical_body)

    op_id = "op-2"
    record_pending_operation(tmp_path, op_id, {"issue": "12", "expected_body_hash": expected_hash})
    receipt = reconcile(op_id, outbox_dir=tmp_path, owner="acme", repo="widgets", issue="12",
                        comment_id=7, expected_body_hash=expected_hash, runner=runner)
    assert receipt["outcome"] == "reconciled"
    assert list_pending_operations(tmp_path) == []  # marked done, not left pending


def test_reconcile_reports_still_pending_when_body_does_not_match(tmp_path):
    canonical = {"id": 7, "user": {"login": "loop-bot"}, "body": "different\n" + LIFECYCLE_COMMENT_MARKER, "created_at": "t"}
    runner, _ = _fake_gh(comments=[canonical])
    op_id = "op-3"
    record_pending_operation(tmp_path, op_id, {"issue": "12"})
    receipt = reconcile(op_id, outbox_dir=tmp_path, owner="acme", repo="widgets", issue="12",
                        comment_id=7, expected_body_hash="deadbeef", runner=runner)
    assert receipt["outcome"] == "still_pending"
    assert len(list_pending_operations(tmp_path)) == 1


def test_reconcile_treats_a_corrupted_outbox_record_as_still_pending(tmp_path):
    from simplicio_loop.github_lifecycle import _outbox_path
    path = _outbox_path(tmp_path, "op-corrupt-record")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")

    canonical = {"id": 7, "user": {"login": "loop-bot"}, "body": "state\n" + LIFECYCLE_COMMENT_MARKER, "created_at": "t"}
    runner, _ = _fake_gh(comments=[canonical])
    receipt = reconcile("op-corrupt-record", outbox_dir=tmp_path, owner="acme", repo="widgets",
                        issue="12", comment_id=7, expected_body_hash="deadbeef", runner=runner)
    assert receipt["outcome"] == "still_pending"


def test_reconcile_unknown_operation_id_is_not_found(tmp_path):
    receipt = reconcile("nope", outbox_dir=tmp_path, owner="acme", repo="widgets", issue="12")
    assert receipt["outcome"] == "not_found"
    assert receipt["reason_code"] == "OPERATION_NOT_FOUND"


def test_reconcile_already_done_short_circuits_without_a_live_requery(tmp_path):
    def runner(cmd, **kw):
        raise AssertionError("an already-done operation must never re-query the source: %r" % (cmd,))

    op_id = "op-already-done"
    record_pending_operation(tmp_path, op_id, {"issue": "12"})
    mark_operation_done(tmp_path, op_id, {"schema": "x", "outcome": "reconciled"})
    receipt = reconcile(op_id, outbox_dir=tmp_path, owner="acme", repo="widgets", issue="12", runner=runner)
    assert receipt["outcome"] == "reconciled"
    assert receipt["receipt"] == {"schema": "x", "outcome": "reconciled"}


def test_reconcile_without_a_comment_id_falls_back_to_the_canonical_comment(tmp_path):
    canonical_body = "state\n" + LIFECYCLE_COMMENT_MARKER
    canonical = {"id": 7, "user": {"login": "loop-bot"}, "body": canonical_body, "created_at": "t"}
    runner, _ = _fake_gh(comments=[canonical])
    from simplicio_loop.github_lifecycle import content_hash
    expected_hash = content_hash(canonical_body)

    op_id = "op-canonical-fallback"
    record_pending_operation(tmp_path, op_id, {"issue": "12"})
    receipt = reconcile(op_id, outbox_dir=tmp_path, owner="acme", repo="widgets", issue="12",
                        expected_body_hash=expected_hash, runner=runner)  # no comment_id given
    assert receipt["outcome"] == "reconciled"
    assert receipt["observed_body_hash"] == expected_hash


def test_mark_operation_done_recovers_from_a_corrupted_existing_record(tmp_path):
    # Simulate a half-written/corrupt outbox record on disk before mark_operation_done runs --
    # it must not crash, just treat it as if there were no prior record.
    from simplicio_loop.github_lifecycle import _outbox_path
    path = _outbox_path(tmp_path, "op-corrupt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    mark_operation_done(tmp_path, "op-corrupt", {"ok": True})
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["status"] == "done"


def test_list_pending_operations_on_a_missing_directory_returns_empty(tmp_path):
    assert list_pending_operations(tmp_path / "does-not-exist") == []


def test_list_pending_operations_skips_corrupted_files(tmp_path):
    record_pending_operation(tmp_path, "op-good", {"issue": "12"})
    (tmp_path / "op-bad.json").write_text("{not valid json", encoding="utf-8")
    pending = list_pending_operations(tmp_path)
    assert [p["operation_id"] for p in pending] == ["op-good"]


# --- lease/fencing-gated publish --------------------------------------------------------


def test_publish_lifecycle_state_blocks_on_lost_lease():
    def _fake_transport_runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    class LeaseLost(RuntimeError):
        pass

    def require_active():
        raise LeaseLost("fencing token stale")

    try:
        publish_lifecycle_state(owner="acme", repo="widgets", issue="12", state="CLAIMED",
                                run_id="run-1", attempt_id="issue-12-1",
                                publish_comment_fn=publish_comment, runner=_fake_transport_runner,
                                require_active=require_active)
    except LeaseLost:
        pass
    else:
        raise AssertionError("expected the lease check to block the write")


def test_publish_lifecycle_state_outbox_records_then_clears_on_success(tmp_path):
    calls = {"n": 0}

    def runner(cmd, **kw):
        if cmd[:2] == ["gh", "api"] and len(cmd) >= 3 and "comments" in cmd[2] and "-X" not in cmd and "/comments/" not in cmd[2]:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        if "-X" in cmd and "POST" in cmd:
            calls["n"] += 1
            body = json.loads(kw.get("input") or "{}").get("body", "")
            runner.body = body  # type: ignore[attr-defined]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"id": 501}), stderr="")
        if "/comments/" in cmd[2]:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"id": 501, "body": getattr(runner, "body", "")}), stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected")

    receipt = publish_lifecycle_state(owner="acme", repo="widgets", issue="12", state="CLAIMED",
                                      run_id="run-1", attempt_id="issue-12-1",
                                      publish_comment_fn=publish_comment, runner=runner,
                                      outbox_dir=tmp_path)
    assert receipt["verified"] is True
    assert list_pending_operations(tmp_path) == []  # cleared after confirmation


# --- close_source_issue (fail-closed) ----------------------------------------------------


def test_close_source_issue_fails_closed_when_gh_close_call_fails():
    runner, _ = _fake_gh(close_ok=False)
    receipt = close_source_issue(owner="acme", repo="widgets", issue="12", run_id="run-1",
                                 attempt_id="issue-12-1", runner=runner)
    assert receipt["outcome"] == "blocked"
    assert receipt["reason_code"] == "SOURCE_CLOSE_FAILED"
    assert receipt["verified"] is False


def test_close_source_issue_fails_closed_when_reopen_state_unconfirmed():
    runner, _ = _fake_gh(close_ok=True, closed_after=False)
    receipt = close_source_issue(owner="acme", repo="widgets", issue="12", run_id="run-1",
                                 attempt_id="issue-12-1", runner=runner)
    assert receipt["outcome"] == "blocked"
    assert receipt["reason_code"] == "SOURCE_CLOSE_UNCONFIRMED"


def test_close_source_issue_succeeds_and_updates_the_canonical_comment():
    def runner(cmd, **kw):
        if cmd[:2] == ["gh", "issue"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["gh", "api"] and len(cmd) >= 3:
            url = cmd[2]
            if "/comments" in url and "/issues/comments/" not in url:
                return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
            if "/issues/comments/" in url:
                cid = int(url.rsplit("/", 1)[-1])
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"id": cid, "body": getattr(runner, "body", "")}), stderr="")
            if "-X" in cmd and "POST" in cmd:
                pass
            if "/issues/" in url:
                data = _issue_payload()
                data["state"] = "closed"
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(data), stderr="")
        if "-X" in cmd and "POST" in cmd:
            body = json.loads(kw.get("input") or "{}").get("body", "")
            runner.body = body  # type: ignore[attr-defined]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"id": 88}), stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected: %r" % (cmd,))

    receipt = close_source_issue(owner="acme", repo="widgets", issue="12", run_id="run-1",
                                 attempt_id="issue-12-1", publish_comment_fn=publish_comment,
                                 runner=runner)
    assert receipt["source_state"] == "closed"
    assert receipt["outcome"] == "closed"
    assert receipt["verified"] is True


def test_close_source_issue_checks_lease_and_clears_outbox_on_success(tmp_path):
    """Covers the `require_active` lease-check call immediately before the `gh issue close`
    mutation, and the outbox record being recorded then marked done on a fully-verified close
    (#285 steps 1/10/13)."""
    def runner(cmd, **kw):
        if cmd[:2] == ["gh", "issue"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["gh", "api"] and len(cmd) >= 3:
            url = cmd[2]
            if "/comments" in url and "/issues/comments/" not in url:
                return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
            if "/issues/comments/" in url:
                cid = int(url.rsplit("/", 1)[-1])
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"id": cid, "body": getattr(runner, "body", "")}), stderr="")
            if "/issues/" in url:
                data = _issue_payload()
                data["state"] = "closed"
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(data), stderr="")
        if "-X" in cmd and "POST" in cmd:
            body = json.loads(kw.get("input") or "{}").get("body", "")
            runner.body = body  # type: ignore[attr-defined]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"id": 88}), stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected: %r" % (cmd,))

    lease_checks = []

    def require_active():
        lease_checks.append(True)

    outbox_dir = tmp_path / "outbox"
    receipt = close_source_issue(owner="acme", repo="widgets", issue="12", run_id="run-1",
                                 attempt_id="issue-12-1", publish_comment_fn=publish_comment,
                                 runner=runner, require_active=require_active,
                                 outbox_dir=outbox_dir)
    assert receipt["outcome"] == "closed"
    assert lease_checks == [True]
    assert list_pending_operations(outbox_dir) == []  # cleared once the close was confirmed


def test_close_source_issue_reports_pending_reconciliation_when_comment_update_fails():
    def runner(cmd, **kw):
        if cmd[:2] == ["gh", "issue"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["gh", "api"] and len(cmd) >= 3:
            url = cmd[2]
            if "/comments" in url and "/issues/comments/" not in url:
                return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
            if "/issues/" in url:
                data = _issue_payload()
                data["state"] = "closed"
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(data), stderr="")
        if "-X" in cmd and "POST" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="rate limited")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected: %r" % (cmd,))

    receipt = close_source_issue(owner="acme", repo="widgets", issue="12", run_id="run-1",
                                 attempt_id="issue-12-1", publish_comment_fn=publish_comment,
                                 runner=runner)
    assert receipt["source_state"] == "closed"  # the issue really did close
    assert receipt["outcome"] == "CLOSE_PENDING_RECONCILIATION"  # but the comment write did not confirm


# --- close_source_issue: planning_snapshot / SOURCE_CHANGED (#285 remaining gap) --------


def _close_runner_factory(*, issue_title="fix the thing", comments=None):
    """Like `_fake_gh`, but also serves a `-X POST` comment create/update, so it can back
    BOTH the `planning_snapshot`-time `get_details()` call and the full `close_source_issue`
    flow (pre-close drift re-query, `gh issue close`, post-close re-query, comment publish)
    with one runner. Returns `(runner, state)`; `state["closed"]` flips only once `gh issue
    close` is actually invoked -- tests assert it stays `False` when the close was blocked."""
    state = {"closed": False}
    comments = list(comments or [])

    def runner(cmd, **kw):
        if cmd[:2] == ["gh", "issue"] and "close" in cmd:
            state["closed"] = True
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["gh", "api"] and len(cmd) >= 3:
            url = cmd[2]
            if "/comments" in url and "/issues/comments/" not in url:
                page = 1
                for part in url.split("&"):
                    if part.startswith("page="):
                        page = int(part.split("=", 1)[1])
                batch = comments if page == 1 else []
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(batch), stderr="")
            if "/issues/comments/" in url:
                cid = int(url.rsplit("/", 1)[-1])
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps({"id": cid, "body": getattr(runner, "body", "")}), stderr="")
            if "/issues/" in url:
                data = _issue_payload(title=issue_title)
                if state["closed"]:
                    data["state"] = "closed"
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(data), stderr="")
        if "-X" in cmd and ("POST" in cmd or "PATCH" in cmd):
            body = json.loads(kw.get("input") or "{}").get("body", "")
            runner.body = body  # type: ignore[attr-defined]
            comment_id = comments[0]["id"] if ("PATCH" in cmd and comments) else 88
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"id": comment_id}), stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected: %r" % (cmd,))

    return runner, state


def test_close_source_issue_succeeds_when_planning_snapshot_matches_current_state():
    """Nothing changed between planning/claim time and close: the close proceeds normally."""
    runner, state = _close_runner_factory()
    planning_snapshot = get_details("acme", "widgets", "12", runner=runner)

    receipt = close_source_issue(owner="acme", repo="widgets", issue="12", run_id="run-1",
                                 attempt_id="issue-12-1", publish_comment_fn=publish_comment,
                                 runner=runner, planning_snapshot=planning_snapshot)
    assert receipt["outcome"] == "closed"
    assert receipt["verified"] is True
    assert state["closed"] is True


def test_close_source_issue_blocks_with_source_changed_when_title_changed():
    """A material human edit (title) between planning and close blocks the close with
    SOURCE_CHANGED and `gh issue close` is never invoked."""
    baseline_runner, _ = _close_runner_factory(issue_title="fix the thing")
    planning_snapshot = get_details("acme", "widgets", "12", runner=baseline_runner)

    close_runner, close_state = _close_runner_factory(issue_title="fix the OTHER thing entirely")
    receipt = close_source_issue(owner="acme", repo="widgets", issue="12", run_id="run-1",
                                 attempt_id="issue-12-1", publish_comment_fn=publish_comment,
                                 runner=close_runner, planning_snapshot=planning_snapshot)
    assert receipt["outcome"] == "blocked"
    assert receipt["reason_code"] == "SOURCE_CHANGED"
    assert receipt["verified"] is False
    assert receipt["drift"]["drifted"] is True
    assert close_state["closed"] is False  # never reached `gh issue close`


def test_close_source_issue_blocks_with_source_changed_when_new_human_comment_appears():
    """A new HUMAN comment posted after the planning snapshot also blocks the close with
    SOURCE_CHANGED, without ever calling `gh issue close`."""
    baseline_runner, _ = _close_runner_factory(comments=[])
    planning_snapshot = get_details("acme", "widgets", "12", runner=baseline_runner)

    new_human_comment = {"id": 5, "user": {"login": "human-reviewer"},
                         "body": "wait, don't close this yet", "created_at": "t"}
    close_runner, close_state = _close_runner_factory(comments=[new_human_comment])
    receipt = close_source_issue(owner="acme", repo="widgets", issue="12", run_id="run-1",
                                 attempt_id="issue-12-1", publish_comment_fn=publish_comment,
                                 runner=close_runner, planning_snapshot=planning_snapshot)
    assert receipt["outcome"] == "blocked"
    assert receipt["reason_code"] == "SOURCE_CHANGED"
    assert close_state["closed"] is False


def test_close_source_issue_does_not_self_drift_on_its_own_canonical_comment_rewrite():
    """The adapter's OWN canonical lifecycle comment is rewritten on every transition
    between planning and close (CLAIMED -> PLANNED -> ... -> CLOSING) -- that must NEVER
    look like a material human edit and block the close."""
    baseline_runner, _ = _close_runner_factory(comments=[])
    planning_snapshot = get_details("acme", "widgets", "12", runner=baseline_runner)

    canonical_comment = {"id": 9, "user": {"login": "loop-bot"},
                         "body": "state: IN_PROGRESS\n" + LIFECYCLE_COMMENT_MARKER, "created_at": "t"}
    close_runner, state = _close_runner_factory(comments=[canonical_comment])
    receipt = close_source_issue(owner="acme", repo="widgets", issue="12", run_id="run-1",
                                 attempt_id="issue-12-1", publish_comment_fn=publish_comment,
                                 runner=close_runner, planning_snapshot=planning_snapshot)
    assert receipt["outcome"] == "closed"
    assert state["closed"] is True


def test_close_source_issue_without_planning_snapshot_skips_the_drift_check():
    """Omitting `planning_snapshot` (the default) is a no-op for this check -- existing
    callers that have not wired a planning-time capture are unaffected."""
    runner, state = _close_runner_factory(issue_title="fix the thing")
    receipt = close_source_issue(owner="acme", repo="widgets", issue="12", run_id="run-1",
                                 attempt_id="issue-12-1", publish_comment_fn=publish_comment,
                                 runner=runner)
    assert receipt["outcome"] == "closed"
    assert state["closed"] is True


# --- verify_issue_state (IssueStateVerifier, #290) --------------------------------------


def test_verify_issue_state_live_open_matches_expected():
    runner, _ = _fake_gh(issue=_issue_payload(state="open"))
    result = verify_issue_state("acme", "widgets", "12", expected_state="open", runner=runner)
    assert result["state"] == "open"
    assert result["source"] == "live"
    assert result["verified"] is True
    assert result["reason_code"] is None


def test_verify_issue_state_mismatch_is_unverified_with_reason_code():
    runner, _ = _fake_gh(issue=_issue_payload(state="closed"))
    result = verify_issue_state("acme", "widgets", "12", expected_state="open", runner=runner)
    assert result["state"] == "closed"
    assert result["verified"] is False
    assert result["reason_code"] == "issue_state_mismatch"
    assert result["expected_state"] == "open"


def test_verify_issue_state_no_expectation_just_reports_observed_state():
    runner, _ = _fake_gh(issue=_issue_payload(state="closed"))
    result = verify_issue_state("acme", "widgets", "12", runner=runner)
    assert result["state"] == "closed"
    assert result["verified"] is True


def test_verify_issue_state_transport_failure_is_unverified_never_falls_back_to_cache():
    def failing_runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 403: Forbidden")

    result = verify_issue_state("acme", "widgets", "12", expected_state="open", runner=failing_runner)
    assert result["verified"] is False
    assert result["state"] is None
    assert result["reason_code"] == "PERMISSION_DENIED"


def test_verify_issue_state_default_ttl_zero_forces_live_requery_even_with_fresh_cache():
    # ttl_seconds defaults to 0 -- "always re-query" -- so even a `cached_snapshot`
    # stamped one second ago must not short-circuit the live call.
    calls = {"n": 0}
    runner, _ = _fake_gh(issue=_issue_payload(state="open"))

    def counting_runner(cmd, **kw):
        calls["n"] += 1
        return runner(cmd, **kw)

    cached = {"state": "closed", "observed_at": "2026-07-15T11:59:59Z"}
    result = verify_issue_state("acme", "widgets", "12", expected_state="open",
                                cached_snapshot=cached, runner=counting_runner)
    assert calls["n"] > 0  # the live path really ran
    assert result["source"] == "live"
    assert result["state"] == "open"  # trusts the live read, not the stale cached "closed"


def test_verify_issue_state_uses_fresh_cache_within_positive_ttl():
    def _boom(cmd, **kw):
        raise AssertionError("must not hit the network when the cache is fresh")

    from simplicio_loop.freshness import now_iso
    cached = {"state": "open", "observed_at": now_iso()}
    result = verify_issue_state("acme", "widgets", "12", expected_state="open",
                                cached_snapshot=cached, ttl_seconds=300, runner=_boom)
    assert result["source"] == "cache"
    assert result["state"] == "open"
    assert result["verified"] is True


# --- close_source_issue with precheck_issue_state (#290 pre/post reconciliation) --------


def test_close_source_issue_precheck_blocks_when_already_closed():
    runner, _ = _fake_gh(issue=_issue_payload(state="closed"))
    receipt = close_source_issue(owner="acme", repo="widgets", issue="12", run_id="run-1",
                                 attempt_id="issue-12-1", runner=runner, precheck_issue_state=True)
    assert receipt["outcome"] == "blocked"
    assert receipt["reason_code"] == "SOURCE_ALREADY_CLOSED"
    assert receipt["precheck"]["state"] == "closed"


def test_close_source_issue_precheck_blocks_on_transport_failure_without_attempting_close():
    close_attempted = {"called": False}

    def failing_view_runner(cmd, **kw):
        if cmd[:2] == ["gh", "issue"] and "close" in cmd:
            close_attempted["called"] = True
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 500")

    receipt = close_source_issue(owner="acme", repo="widgets", issue="12", run_id="run-1",
                                 attempt_id="issue-12-1", runner=failing_view_runner,
                                 precheck_issue_state=True)
    assert receipt["outcome"] == "blocked"
    assert receipt["reason_code"] == "SOURCE_STATE_PRECHECK_FAILED"
    assert close_attempted["called"] is False  # never reached the mutation


def test_close_source_issue_precheck_passes_when_open_and_proceeds_to_close():
    runner, _ = _fake_gh(issue=_issue_payload(state="open"), close_ok=True, closed_after=True)
    receipt = close_source_issue(owner="acme", repo="widgets", issue="12", run_id="run-1",
                                 attempt_id="issue-12-1", runner=runner, precheck_issue_state=True)
    assert receipt["source_state"] == "closed"
    assert receipt.get("reason_code") not in ("SOURCE_ALREADY_CLOSED", "SOURCE_STATE_PRECHECK_FAILED")
