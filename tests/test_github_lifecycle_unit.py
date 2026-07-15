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
    SUPERSEDED_MARKER,
    GitHubTransportError,
    _classify_gh_failure,
    _paginated_gh_api,
    content_hash,
    elect_canonical_comment,
    find_marker_comments,
    get_authenticated_login,
    list_ready,
    load_lifecycle_receipt,
    mark_comment_superseded,
    operation_id,
    persist_lifecycle_receipt,
    publish_lifecycle_state,
    reconcile_duplicate_comments,
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


def test_render_includes_every_optional_section_when_given():
    body = render_lifecycle_comment(
        state="IN_PROGRESS", run_id="r", attempt_id="a", runtime="claude", device="macbook-a",
        branch="feat/x", worktree="worker-a", updated_at="2026-01-01T00:00:00Z",
        scope="only the login flow", plan_steps=["STEP-001 do a thing", "STEP-002 do another"],
        delivery="PR #99 opened",
    )
    assert "| Runtime / device | claude / macbook-a |" in body
    assert "| Branch / worktree | feat/x / worker-a |" in body
    assert "| Atualizado | 2026-01-01T00:00:00Z |" in body
    assert "only the login flow" in body
    assert "1. STEP-001 do a thing" in body
    assert "2. STEP-002 do another" in body
    assert "### Entrega" in body and "PR #99 opened" in body


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


def test_re_query_with_non_json_comment_body_is_reported_as_unverified():
    # The re-query step fetches the comment back by id after publishing; if that fetch
    # returns unparsable JSON (a transient gh/gateway glitch), it must be treated as "no
    # observed body" -- reported as verified=False, never a crash or a fake pass.
    def runner(cmd, **kw):
        if cmd[:2] == ["gh", "api"] and len(cmd) >= 3 and "comments" in cmd[2] and "-X" not in cmd and "/comments/" not in cmd[2]:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        if "-X" in cmd and "POST" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"id": 321}), stderr="")
        if "/comments/" in cmd[2]:
            return subprocess.CompletedProcess(cmd, 0, stdout="not json at all", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected")

    receipt = publish_lifecycle_state(owner="acme", repo="widgets", issue="12", state="CLAIMED",
                                      run_id="run-1", attempt_id="issue-12-1",
                                      publish_comment_fn=publish_comment, runner=runner)
    assert receipt["verified"] is False
    assert receipt["observed_body_hash"] == ""


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


# --- duplicate-comment election (#285 "Recuperação de duplicatas") -------------------


_MARKER_BODY = "status body\n" + LIFECYCLE_COMMENT_MARKER


def test_find_marker_comments_filters_out_non_marker_comments():
    comments = [
        {"id": 1, "body": _MARKER_BODY, "user": {"login": "bot-a"}},
        {"id": 2, "body": "just a human reply, no marker", "user": {"login": "human"}},
        {"id": 3, "body": _MARKER_BODY, "user": {"login": "bot-a"}},
    ]
    found = find_marker_comments(comments)
    assert [c["id"] for c in found] == [1, 3]


def test_elect_canonical_comment_picks_lowest_id():
    matching = [
        {"id": 42, "body": _MARKER_BODY, "user": {"login": "bot-a"}},
        {"id": 7, "body": _MARKER_BODY, "user": {"login": "bot-b"}},
        {"id": 99, "body": _MARKER_BODY, "user": {"login": "bot-a"}},
    ]
    canonical, duplicates = elect_canonical_comment(matching)
    assert canonical["id"] == 7
    assert [d["id"] for d in duplicates] == [42, 99]


def test_elect_canonical_comment_empty_input():
    canonical, duplicates = elect_canonical_comment([])
    assert canonical is None
    assert duplicates == []


def test_mark_comment_superseded_never_touches_a_foreign_author():
    def runner(cmd, **kw):
        raise AssertionError("must never call gh for a foreign-author comment: %r" % (cmd,))

    result = mark_comment_superseded(
        "acme", "widgets", {"id": 5, "body": _MARKER_BODY, "user": {"login": "someone-else"}},
        own_login="bot-a", runner=runner,
    )
    assert result == {"comment_id": 5, "author": "someone-else", "action": "skipped_foreign_author"}


def test_mark_comment_superseded_is_idempotent_on_already_superseded_body():
    already = _MARKER_BODY + "\n" + SUPERSEDED_MARKER

    def runner(cmd, **kw):
        raise AssertionError("must never re-PATCH an already-superseded comment: %r" % (cmd,))

    result = mark_comment_superseded(
        "acme", "widgets", {"id": 5, "body": already, "user": {"login": "bot-a"}},
        own_login="bot-a", runner=runner,
    )
    assert result["action"] == "already_superseded"


def test_mark_comment_superseded_edits_own_duplicate_comment():
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        assert cmd[:4] == ["gh", "api", "-X", "PATCH"]
        body = json.loads(kw["input"])["body"]
        assert SUPERSEDED_MARKER in body
        assert _MARKER_BODY in body
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    result = mark_comment_superseded(
        "acme", "widgets", {"id": 5, "body": _MARKER_BODY, "user": {"login": "bot-a"}},
        own_login="bot-a", runner=runner,
    )
    assert result == {"comment_id": 5, "author": "bot-a", "action": "marked_superseded"}
    assert len(calls) == 1


def test_mark_comment_superseded_reports_typed_reason_code_on_gh_failure():
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 403: forbidden")

    result = mark_comment_superseded(
        "acme", "widgets", {"id": 5, "body": _MARKER_BODY, "user": {"login": "bot-a"}},
        own_login="bot-a", runner=runner,
    )
    assert result["action"] == "failed"
    assert result["reason_code"] == "PERMISSION_DENIED"


def test_get_authenticated_login_parses_login_from_gh_api_user():
    def runner(cmd, **kw):
        assert cmd == ["gh", "api", "user"]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"login": "bot-a"}), stderr="")

    assert get_authenticated_login(runner=runner) == "bot-a"


def test_get_authenticated_login_returns_none_on_failure_or_bad_json():
    def failing_runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="401 unauthorized")

    assert get_authenticated_login(runner=failing_runner) is None

    def bad_json_runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="not json", stderr="")

    assert get_authenticated_login(runner=bad_json_runner) is None


def _fake_duplicate_transport(comments, own_login="bot-a"):
    """A fake runner servicing: `gh api user`, paginated comment listing, and PATCH."""
    state = {"comments": {c["id"]: dict(c) for c in comments}}

    def runner(cmd, **kw):
        if cmd == ["gh", "api", "user"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"login": own_login}), stderr="")
        if cmd[:2] == ["gh", "api"] and "comments" in cmd[2] and "-X" not in cmd:
            listing = list(state["comments"].values())
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(listing), stderr="")
        if "-X" in cmd and "PATCH" in cmd:
            url = next(part for part in cmd if part.startswith("repos/"))
            comment_id = int(url.rsplit("/", 1)[-1])
            body = json.loads(kw["input"])["body"]
            state["comments"][comment_id]["body"] = body
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected call: %r" % (cmd,))

    return runner, state


def test_reconcile_duplicate_comments_elects_and_supersedes_own_duplicate():
    comments = [
        {"id": 10, "body": _MARKER_BODY, "user": {"login": "bot-a"}},
        {"id": 20, "body": _MARKER_BODY, "user": {"login": "bot-a"}},
        {"id": 30, "body": "human comment, no marker", "user": {"login": "human"}},
    ]
    runner, state = _fake_duplicate_transport(comments)
    receipt = reconcile_duplicate_comments("acme", "widgets", "12", own_login="bot-a", runner=runner)
    assert receipt["schema"] == "simplicio.github-duplicate-election/v1"
    assert receipt["canonical_comment_id"] == 10
    assert receipt["duplicate_count"] == 1
    assert receipt["actions"] == [{"comment_id": 20, "author": "bot-a", "action": "marked_superseded"}]
    assert SUPERSEDED_MARKER in state["comments"][20]["body"]
    assert SUPERSEDED_MARKER not in state["comments"][10]["body"]  # canonical is never touched


def test_reconcile_duplicate_comments_resolves_own_login_when_not_supplied():
    comments = [
        {"id": 1, "body": _MARKER_BODY, "user": {"login": "bot-a"}},
        {"id": 2, "body": _MARKER_BODY, "user": {"login": "bot-a"}},
    ]
    runner, state = _fake_duplicate_transport(comments, own_login="bot-a")
    receipt = reconcile_duplicate_comments("acme", "widgets", "12", runner=runner)
    assert receipt["actions"][0]["action"] == "marked_superseded"


def test_reconcile_duplicate_comments_never_edits_a_different_authors_duplicate():
    comments = [
        {"id": 1, "body": _MARKER_BODY, "user": {"login": "legacy-human"}},
        {"id": 2, "body": _MARKER_BODY, "user": {"login": "bot-a"}},
    ]
    runner, state = _fake_duplicate_transport(comments, own_login="bot-a")
    receipt = reconcile_duplicate_comments("acme", "widgets", "12", own_login="bot-a", runner=runner)
    # id 1 is canonical (lowest id) and is never touched either way; id 2 is a duplicate
    # authored by bot-a itself, so it IS editable.
    assert receipt["canonical_comment_id"] == 1
    assert receipt["actions"] == [{"comment_id": 2, "author": "bot-a", "action": "marked_superseded"}]


def test_reconcile_duplicate_comments_no_marker_comments_at_all():
    runner, _ = _fake_duplicate_transport([])
    receipt = reconcile_duplicate_comments("acme", "widgets", "12", own_login="bot-a", runner=runner)
    assert receipt["canonical_comment_id"] is None
    assert receipt["duplicate_count"] == 0
    assert receipt["actions"] == []


# --- _classify_gh_failure / _paginated_gh_api ----------------------------------------


def test_classify_gh_failure_maps_http_status_substrings():
    assert _classify_gh_failure(1, "HTTP 401: Bad credentials") == "AUTH_REQUIRED"
    assert _classify_gh_failure(1, "HTTP 403: Forbidden") == "PERMISSION_DENIED"
    assert _classify_gh_failure(1, "HTTP 404: Not Found") == "SOURCE_NOT_FOUND"
    assert _classify_gh_failure(1, "HTTP 409: Conflict") == "CLAIM_CONFLICT"
    assert _classify_gh_failure(1, "HTTP 422: Unprocessable") == "SOURCE_CHANGED"
    assert _classify_gh_failure(1, "HTTP 429: Too Many Requests") == "RATE_LIMITED"


def test_classify_gh_failure_maps_rate_limit_and_network_text_without_a_status_code():
    assert _classify_gh_failure(1, "API rate limit exceeded for user") == "RATE_LIMITED"
    assert _classify_gh_failure(1, "could not resolve host: api.github.com") == "NETWORK_UNAVAILABLE"
    assert _classify_gh_failure(1, "context deadline exceeded (Client.Timeout)") == "NETWORK_UNAVAILABLE"
    assert _classify_gh_failure(1, "connection reset by peer") == "NETWORK_UNAVAILABLE"
    assert _classify_gh_failure(1, "totally unrecognized failure text") == "NETWORK_UNAVAILABLE"


def test_github_transport_error_falls_back_on_an_unknown_reason_code():
    exc = GitHubTransportError("NOT_A_REAL_REASON_CODE", "boom")
    assert exc.reason_code == "NETWORK_UNAVAILABLE"


def test_paginated_gh_api_follows_multiple_pages_until_a_short_page():
    pages = [
        [{"id": i} for i in range(100)],
        [{"id": i} for i in range(100, 150)],
    ]
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        page_no = len(calls)
        batch = pages[page_no - 1] if page_no <= len(pages) else []
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(batch), stderr="")

    items = _paginated_gh_api("repos/acme/widgets/issues", runner=runner, timeout=5)
    assert len(items) == 150
    assert len(calls) == 2  # stops once a page comes back shorter than per_page


def test_paginated_gh_api_raises_typed_error_on_gh_failure():
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 404: Not Found")

    try:
        _paginated_gh_api("repos/acme/widgets/issues", runner=runner, timeout=5)
        raise AssertionError("expected GitHubTransportError")
    except GitHubTransportError as exc:
        assert exc.reason_code == "SOURCE_NOT_FOUND"


def test_paginated_gh_api_raises_typed_error_on_non_json_page():
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="not json at all", stderr="")

    try:
        _paginated_gh_api("repos/acme/widgets/issues", runner=runner, timeout=5)
        raise AssertionError("expected GitHubTransportError")
    except GitHubTransportError as exc:
        assert exc.reason_code == "NETWORK_UNAVAILABLE"


def test_paginated_gh_api_treats_a_non_list_page_as_empty():
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"not": "a list"}), stderr="")

    items = _paginated_gh_api("repos/acme/widgets/issues", runner=runner, timeout=5)
    assert items == []


def test_list_ready_applies_the_milestone_filter_to_the_query_path():
    seen_urls = []

    def runner(cmd, **kw):
        seen_urls.append(cmd[2])
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    list_ready("acme", "widgets", milestone="v2", runner=runner)
    assert any("milestone=v2" in url for url in seen_urls)


# --- lifecycle receipt persistence ----------------------------------------------------


def test_load_lifecycle_receipt_returns_none_when_absent(tmp_path):
    assert load_lifecycle_receipt(tmp_path) is None


def test_persist_then_load_lifecycle_receipt_round_trips(tmp_path):
    receipt = {"schema": "simplicio.github-lifecycle-receipt/v1", "outcome": "closed"}
    persist_lifecycle_receipt(receipt, tmp_path)
    loaded = load_lifecycle_receipt(tmp_path)
    assert loaded == receipt


def test_load_lifecycle_receipt_returns_none_on_corrupted_file(tmp_path):
    from simplicio_loop.github_lifecycle import lifecycle_receipt_path
    path = lifecycle_receipt_path(tmp_path)
    path.write_text("{not valid json", encoding="utf-8")
    assert load_lifecycle_receipt(tmp_path) is None
