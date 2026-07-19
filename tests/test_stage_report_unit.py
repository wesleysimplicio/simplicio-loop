"""Unit tests for `simplicio_loop/stage_report.py` (#433 "Portable Stage Agents" + #442
identity/idempotency spec).

Covers: agent-identity formatting (`Name/Role - #XXXX - Model`, 4-char hostname), idempotency-key
stability across retries, marker-based find-and-update (via a fake `gh` runner routed through the
REAL `scripts.pr_evidence.publish_comment` primitive -- proving `publish_stage_report` genuinely
queries-before-deciding rather than only exercising a standalone helper), status-tag correctness,
and content sanitization/length limits.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from pr_evidence import PublishError, publish_comment  # noqa: E402

from simplicio_loop.stage_report import (
    STATUS_TAGS,
    build_marker,
    content_hash,
    format_agent_identity,
    hostname_abbrev,
    idempotency_key,
    publish_stage_report,
    render_stage_report,
    sanitize,
    truncate_body,
)


# ---- identity ------------------------------------------------------------------------------

def test_hostname_abbrev_always_4_chars_uppercase():
    assert hostname_abbrev("devbox01") == "DEVB"
    assert hostname_abbrev("a1") == "A1XX"
    assert len(hostname_abbrev("")) == 4


def test_hostname_abbrev_strips_domain_and_non_alnum():
    assert hostname_abbrev("my-host.corp.local") == "MYHO"


def test_format_agent_identity_matches_442_spec():
    ident = format_agent_identity("Claude", "Implementer", "claude-sonnet-5", hostname="devbox01")
    assert ident == "Claude/Implementer - #DEVB - claude-sonnet-5"


def test_format_agent_identity_differs_across_hosts_same_model():
    a = format_agent_identity("Claude", "Implementer", "claude-sonnet-5", hostname="alpha01")
    b = format_agent_identity("Claude", "Implementer", "claude-sonnet-5", hostname="beta02")
    assert a != b


def test_format_agent_identity_defaults_never_blank():
    ident = format_agent_identity("", "", "")
    assert ident  # never empty even with no inputs
    assert " - #" in ident


# ---- idempotency key ------------------------------------------------------------------------

def test_idempotency_key_stable_across_retries():
    k1 = idempotency_key("run-1", "T1", "implementation", 1, "retry")
    k2 = idempotency_key("run-1", "T1", "implementation", 1, "retry")
    assert k1 == k2


def test_idempotency_key_changes_with_any_component():
    base = idempotency_key("run-1", "T1", "implementation", 1, "retry")
    assert idempotency_key("run-2", "T1", "implementation", 1, "retry") != base
    assert idempotency_key("run-1", "T2", "implementation", 1, "retry") != base
    assert idempotency_key("run-1", "T1", "review", 1, "retry") != base
    assert idempotency_key("run-1", "T1", "implementation", 2, "retry") != base
    assert idempotency_key("run-1", "T1", "implementation", 1, "advance") != base


# ---- marker ---------------------------------------------------------------------------------

def test_marker_stable_per_run_and_item():
    assert build_marker("r1", "T1") == build_marker("r1", "T1")


def test_marker_differs_across_items_and_runs():
    assert build_marker("r1", "T1") != build_marker("r1", "T2")
    assert build_marker("r1", "T1") != build_marker("r2", "T1")


# ---- renderer / status tags -----------------------------------------------------------------

def test_render_rejects_unknown_status():
    try:
        render_stage_report(run_id="r1", item="T1", stage="review",
                            agent_identity="x", status="WEIRD")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_render_accepts_all_known_status_tags():
    for status in STATUS_TAGS:
        body = render_stage_report(run_id="r1", item="T1", stage="review",
                                   agent_identity="x", status=status)
        assert ("**%s**" % status) in body


def test_render_includes_marker_and_cross_links():
    body = render_stage_report(run_id="r1", item="T1", stage="delivery", agent_identity="x",
                               status="PASS", issue="12", pr="34", commit="deadbeef")
    assert build_marker("r1", "T1") in body
    assert "Issue #12" in body
    assert "PR #34" in body
    assert "deadbeef" in body


def test_render_is_deterministic_for_same_inputs():
    kwargs = dict(run_id="r1", item="T1", stage="review", agent_identity="x", status="PASS",
                  updated_at="2026-01-01T00:00:00Z", idem_key="fixedkey")
    assert render_stage_report(**kwargs) == render_stage_report(**kwargs)


# ---- sanitization / truncation ---------------------------------------------------------------

def test_sanitize_redacts_token_and_secret_fields():
    assert "[REDACTED-TOKEN]" in sanitize("token here: ghp_" + "a" * 30)
    assert "[REDACTED]" in sanitize("api_key: super-secret-value")
    assert "[REDACTED]" in sanitize("password=hunter2")


def test_sanitize_redacts_signed_url():
    url = "https://example.com/f?X-Amz-Signature=abc123def"
    assert "[REDACTED-SIGNED-URL]" in sanitize(url)


def test_sanitize_passthrough_for_clean_text():
    assert sanitize("all good here") == "all good here"


def test_truncate_body_caps_length():
    long_body = "x" * 1000
    truncated = truncate_body(long_body, max_chars=100)
    assert len(truncated) <= 130
    assert "truncated" in truncated


def test_truncate_body_passthrough_under_cap():
    assert truncate_body("short", max_chars=100) == "short"


# ---- publish path: idempotency GENUINELY wired, not just unit-tested standalone --------------

def test_publish_stage_report_wires_marker_into_publish_comment_fn():
    seen = {}

    def fake_publish_comment_fn(owner, repo, number, body, marker=None, runner=None, timeout=20):
        seen["marker"] = marker
        return {"action": "created", "id": 1}

    publish_stage_report(owner="acme", repo="widgets", target_number="12", run_id="r1",
                         item="T1", stage="delivery", agent_identity="x", status="PASS",
                         publish_comment_fn=fake_publish_comment_fn)
    assert seen["marker"] == build_marker("r1", "T1")


def test_publish_stage_report_creates_via_real_primitive_when_absent():
    def gh_no_existing(cmd, **kw):
        if cmd[:2] == ["gh", "api"] and "comments" in cmd[2] and "-X" not in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        if "-X" in cmd and "POST" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"id": 555}), stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected")

    receipt = publish_stage_report(owner="acme", repo="widgets", target_number="12", run_id="r1",
                                   item="T1", stage="delivery", agent_identity="x", status="PASS",
                                   publish_comment_fn=publish_comment, runner=gh_no_existing)
    assert receipt["action"] == "created"
    assert receipt["comment_id"] == 555


def test_publish_stage_report_updates_same_id_on_retry_no_duplicate():
    marker = build_marker("r1", "T1")
    calls = []

    def gh_with_existing(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["gh", "api"] and "comments" in cmd[2] and "-X" not in cmd:
            marked = [{"id": 888, "body": "old\n\n" + marker + "\n"}]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(marked), stderr="")
        if "-X" in cmd and "PATCH" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected")

    receipt = publish_stage_report(owner="acme", repo="widgets", target_number="12", run_id="r1",
                                   item="T1", stage="delivery", agent_identity="x",
                                   status="REGRESSED", publish_comment_fn=publish_comment,
                                   runner=gh_with_existing)
    assert receipt["action"] == "updated"
    assert receipt["comment_id"] == 888
    assert not any("-X" in c and "POST" in c for c in calls)


def test_publish_stage_report_verified_only_after_matching_requery():
    def fake_publish_comment_fn(owner, repo, number, body, marker=None, runner=None, timeout=20):
        return {"action": "created", "id": 42}

    receipt_no_requery = publish_stage_report(
        owner="acme", repo="widgets", target_number="12", run_id="r1", item="T1",
        stage="delivery", agent_identity="x", status="PASS",
        publish_comment_fn=fake_publish_comment_fn)
    assert receipt_no_requery["verified"] is False

    def matching_get_body(owner, repo, comment_id, runner, timeout):
        return render_stage_report(run_id="r1", item="T1", stage="delivery",
                                   agent_identity="x", status="PASS")

    receipt_verified = publish_stage_report(
        owner="acme", repo="widgets", target_number="12", run_id="r1", item="T1",
        stage="delivery", agent_identity="x", status="PASS",
        publish_comment_fn=fake_publish_comment_fn, get_comment_body_fn=matching_get_body)
    assert receipt_verified["verified"] is True


def test_publish_stage_report_propagates_publish_error_fail_closed():
    def failing_publish_comment_fn(owner, repo, number, body, marker=None, runner=None, timeout=20):
        raise PublishError("boom")

    try:
        publish_stage_report(owner="acme", repo="widgets", target_number="12", run_id="r1",
                             item="T1", stage="delivery", agent_identity="x", status="PASS",
                             publish_comment_fn=failing_publish_comment_fn)
        assert False, "expected PublishError to propagate"
    except PublishError:
        pass


def test_content_hash_stable():
    assert content_hash("same") == content_hash("same")
    assert content_hash("a") != content_hash("b")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _selfrun import run_module
    run_module(globals(), "test_stage_report_unit")
