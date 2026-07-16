"""Tests for the portable multi-tracker stage-reporting interface (#436)."""
from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from simplicio_loop import multi_tracker_reporting as mtr


def _envelope(**overrides):
    defaults = dict(
        run_id="run-1", task_id="task-1", source="github", stage="implementation",
        agent="agent-a", attempt=1, fence="fence-1", status="COMPLETE", sequence=1,
    )
    defaults.update(overrides)
    return mtr.StageEventEnvelope(**defaults)


# --------------------------------------------------------------------------- #
# Capability detection
# --------------------------------------------------------------------------- #
def test_stub_providers_report_not_connected_not_error():
    for cls in (mtr.AzureDevOpsStubProvider, mtr.JiraStubProvider,
                mtr.AsanaStubProvider, mtr.TrelloStubProvider):
        cap = cls().detect()
        assert cap.state == "NOT_CONNECTED"
        assert cap.reason_code  # must be auditable, never silent
        assert cap.connected is False


def test_fake_provider_disconnected_reports_not_connected():
    fake = mtr.FakeReportingProvider("jira", connected=False)
    cap = fake.detect()
    assert cap.state == "NOT_CONNECTED"
    assert cap.reason_code == "no_connector_configured"


def test_fake_provider_connected_reports_connected_with_probe_fields():
    fake = mtr.FakeReportingProvider("jira", connected=True)
    cap = fake.detect()
    assert cap.state == "CONNECTED"
    assert cap.target_resolved is True
    assert cap.auth_probed is True
    assert cap.can_create_comment is True


def test_capability_requires_reason_code_when_not_connected():
    with pytest.raises(mtr.ReportingError):
        mtr.ReportingCapability(provider="github", state="MISCONFIGURED")


def test_capability_rejects_unknown_provider():
    with pytest.raises(mtr.ReportingError):
        mtr.ReportingCapability(provider="bitbucket", state="CONNECTED")


# --------------------------------------------------------------------------- #
# GitHub-required policy
# --------------------------------------------------------------------------- #
def test_github_never_skippable_when_source_is_github():
    github = mtr.FakeReportingProvider("github", connected=False, required=True)
    dispatcher = mtr.ReportingDispatcher([github])
    env = _envelope(source="github")
    results = dispatcher.dispatch(env, targets={"github": "issue-1"})
    assert results["github"].status == "blocked"
    verdict = dispatcher.completion_verdict(results, github_required=True)
    assert verdict == "blocked"


def test_github_confirmed_yields_confirmed_verdict():
    github = mtr.FakeReportingProvider("github", connected=True)
    dispatcher = mtr.ReportingDispatcher([github])
    env = _envelope(source="github")
    results = dispatcher.dispatch(env, targets={"github": "issue-1"})
    assert results["github"].status == "confirmed"
    assert dispatcher.completion_verdict(results, github_required=True) == "confirmed"


def test_non_github_source_does_not_require_github_confirmation():
    dispatcher = mtr.ReportingDispatcher([mtr.AzureDevOpsStubProvider()])
    env = _envelope(source="local")
    results = dispatcher.dispatch(env, targets={})
    verdict = dispatcher.completion_verdict(results, github_required=False)
    assert verdict != "blocked"


# --------------------------------------------------------------------------- #
# Dispatcher: skip disconnected providers without any remote call
# --------------------------------------------------------------------------- #
def test_disconnected_optional_provider_is_skipped_no_remote_call():
    azure = mtr.AzureDevOpsStubProvider()  # detect() -> NOT_CONNECTED
    dispatcher = mtr.ReportingDispatcher([azure])
    env = _envelope(source="local")
    results = dispatcher.dispatch(env, targets={"azure_devops": "wi-1"})
    assert results["azure_devops"].status == "skipped_not_connected"
    # publish()/find_existing() on the stub raise if ever invoked (guard rail);
    # dispatch() completing without raising proves they were never called.


def test_disconnected_provider_publish_would_raise_if_misused():
    azure = mtr.AzureDevOpsStubProvider()
    with pytest.raises(mtr.ReportingError):
        azure.publish(_envelope(), "wi-1")
    with pytest.raises(mtr.ReportingError):
        azure.find_existing("some-key")


def test_one_disconnected_provider_does_not_block_others():
    github = mtr.FakeReportingProvider("github", connected=True)
    azure = mtr.AzureDevOpsStubProvider()
    dispatcher = mtr.ReportingDispatcher([github, azure])
    env = _envelope(source="github")
    results = dispatcher.dispatch(env, targets={"github": "issue-1", "azure_devops": "wi-1"})
    assert results["github"].status == "confirmed"
    assert results["azure_devops"].status == "skipped_not_connected"
    assert dispatcher.completion_verdict(results, github_required=True) == "confirmed"


def test_connected_provider_without_target_is_not_attempted():
    fake = mtr.FakeReportingProvider("trello", connected=True)
    dispatcher = mtr.ReportingDispatcher([fake])
    env = _envelope(source="local")
    results = dispatcher.dispatch(env, targets={})  # no target configured
    assert results["trello"].status == "skipped_not_connected"
    assert "publish" not in fake.calls


# --------------------------------------------------------------------------- #
# Idempotency per provider+target
# --------------------------------------------------------------------------- #
def test_same_run_task_provider_target_updates_not_duplicates():
    fake = mtr.FakeReportingProvider("jira")
    dispatcher = mtr.ReportingDispatcher([fake])
    env1 = _envelope(source="local", stage="implementation", status="running", sequence=1)
    env2 = _envelope(source="local", stage="review", status="COMPLETE", sequence=2)

    r1 = dispatcher.dispatch(env1, targets={"jira": "ISSUE-9"})
    r2 = dispatcher.dispatch(env2, targets={"jira": "ISSUE-9"})

    assert r1["jira"].remote_comment_id == r2["jira"].remote_comment_id
    assert r1["jira"].body_hash != r2["jira"].body_hash  # content did change
    assert len(fake._store) == 1  # exactly one living comment for this key


def test_different_target_gets_a_distinct_comment():
    fake = mtr.FakeReportingProvider("jira")
    dispatcher = mtr.ReportingDispatcher([fake])
    env = _envelope(source="local")
    r1 = dispatcher.dispatch(env, targets={"jira": "ISSUE-9"})
    r2 = dispatcher.dispatch(env, targets={"jira": "ISSUE-10"})
    assert r1["jira"].remote_comment_id != r2["jira"].remote_comment_id


def test_stale_event_high_water_mark_does_not_overwrite_newer_state():
    fake = mtr.FakeReportingProvider("asana")
    dispatcher = mtr.ReportingDispatcher([fake])
    newer = _envelope(source="local", sequence=5, status="COMPLETE")
    older = _envelope(source="local", sequence=1, status="running")

    r_new = dispatcher.dispatch(newer, targets={"asana": "task-1"})
    r_old = dispatcher.dispatch(older, targets={"asana": "task-1"})

    assert r_old["asana"].body_hash == r_new["asana"].body_hash
    assert "stale" in r_old["asana"].detail


# --------------------------------------------------------------------------- #
# Envelope validation
# --------------------------------------------------------------------------- #
def test_validate_envelope_rejects_missing_fields():
    ok, errors = mtr.validate_envelope({"run_id": "r1"})
    assert not ok
    assert errors


def test_dispatch_rejects_invalid_envelope():
    dispatcher = mtr.ReportingDispatcher([mtr.FakeReportingProvider("github")])
    bad = mtr.StageEventEnvelope(run_id="", task_id="", source="", stage="", agent="",
                                  attempt=1, fence="", status="")
    with pytest.raises(mtr.ReportingError):
        dispatcher.dispatch(bad, targets={})


# --------------------------------------------------------------------------- #
# default_dispatcher wiring
# --------------------------------------------------------------------------- #
def test_default_dispatcher_includes_all_conditional_stubs():
    dispatcher = mtr.default_dispatcher()
    names = set(dispatcher.providers())
    assert names == {"azure_devops", "jira", "asana", "trello"}


def test_default_dispatcher_with_github_provider():
    github = mtr.FakeReportingProvider("github", connected=True)
    dispatcher = mtr.default_dispatcher(github_provider=github)
    assert "github" in dispatcher.providers()
