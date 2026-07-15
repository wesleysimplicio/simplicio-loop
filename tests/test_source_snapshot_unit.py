"""Unit tests for #284 item 1: GitHub source-revision capture.

`simplicio_loop/source_snapshot.py` captures a content-addressed snapshot of one
GitHub issue via `gh issue view` (fail-closed on any `gh` failure) and provides
`detect_source_drift()` for comparing two snapshots by hash -- the primitive
`planning_gate.py` uses to fold `source.snapshot_hash` into the mutation-
authority identity tuple.
"""
import json

import pytest

from simplicio_loop.source_snapshot import (
    SOURCE_SNAPSHOT_SCHEMA,
    capture_github_issue_snapshot,
    detect_source_drift,
    snapshot_hash_of,
)


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_runner(stdout_obj, returncode=0, stderr=""):
    def runner(*args, **kwargs):
        return _FakeCompleted(returncode=returncode, stdout=json.dumps(stdout_obj), stderr=stderr)
    return runner


def _issue_payload(body="original body", comments=None, updated_at="2026-01-01T00:00:00Z"):
    return {
        "title": "feat: something",
        "body": body,
        "labels": [{"name": "bug"}, {"name": "epic"}],
        "milestone": {"title": "v1"},
        "assignees": [{"login": "wesleysimplicio"}],
        "comments": comments or [{"id": 1, "author": {"login": "bot"}, "body": "first comment"}],
        "updatedAt": updated_at,
        "number": 284,
        "url": "https://github.com/acme/repo/issues/284",
    }


def test_capture_github_issue_snapshot_shape_and_schema():
    runner = _fake_runner(_issue_payload())
    snap = capture_github_issue_snapshot("acme/repo", "284", runner=runner, observed_at="2026-02-02T00:00:00Z")
    assert snap["schema"] == SOURCE_SNAPSHOT_SCHEMA
    source = snap["source"]
    assert source["provider"] == "github"
    assert source["repo"] == "acme/repo"
    assert source["item_id"] == "284"
    assert source["url"].endswith("/issues/284")
    assert source["observed_at"] == "2026-02-02T00:00:00Z"
    assert source["snapshot_hash"]
    assert "2026-01-01T00:00:00Z" in source["revision"]
    assert "comments=1" in source["revision"]


def test_capture_github_issue_snapshot_hash_deterministic_for_same_content():
    runner = _fake_runner(_issue_payload())
    a = capture_github_issue_snapshot("acme/repo", "284", runner=runner)
    b = capture_github_issue_snapshot("acme/repo", "284", runner=runner)
    assert a["source"]["snapshot_hash"] == b["source"]["snapshot_hash"]


def test_capture_github_issue_snapshot_hash_changes_on_body_edit():
    a = capture_github_issue_snapshot("acme/repo", "284", runner=_fake_runner(_issue_payload(body="original body")))
    b = capture_github_issue_snapshot("acme/repo", "284", runner=_fake_runner(_issue_payload(body="edited body")))
    assert a["source"]["snapshot_hash"] != b["source"]["snapshot_hash"]


def test_capture_github_issue_snapshot_hash_changes_on_new_comment():
    a = capture_github_issue_snapshot("acme/repo", "284", runner=_fake_runner(_issue_payload()))
    b = capture_github_issue_snapshot(
        "acme/repo", "284",
        runner=_fake_runner(_issue_payload(comments=[
            {"id": 1, "author": {"login": "bot"}, "body": "first comment"},
            {"id": 2, "author": {"login": "human"}, "body": "a new decision was made"},
        ])),
    )
    assert a["source"]["snapshot_hash"] != b["source"]["snapshot_hash"]


def test_capture_github_issue_snapshot_hash_stable_across_label_order():
    def payload_with_labels(order):
        p = _issue_payload()
        p["labels"] = [{"name": name} for name in order]
        return p

    a = capture_github_issue_snapshot("acme/repo", "284", runner=_fake_runner(payload_with_labels(["bug", "epic"])))
    b = capture_github_issue_snapshot("acme/repo", "284", runner=_fake_runner(payload_with_labels(["epic", "bug"])))
    assert a["source"]["snapshot_hash"] == b["source"]["snapshot_hash"]


def test_capture_github_issue_snapshot_raises_on_gh_failure():
    runner = _fake_runner({}, returncode=1, stderr="gh: not found")
    with pytest.raises(RuntimeError, match="gh issue view failed"):
        capture_github_issue_snapshot("acme/repo", "284", runner=runner)


def test_capture_github_issue_snapshot_raises_on_invalid_json():
    def runner(*args, **kwargs):
        return _FakeCompleted(returncode=0, stdout="{not json")
    with pytest.raises(RuntimeError, match="invalid JSON"):
        capture_github_issue_snapshot("acme/repo", "284", runner=runner)


def test_capture_github_issue_snapshot_uses_fixture_env_without_gh(monkeypatch):
    monkeypatch.setenv(
        "SIMPLICIO_LOOP_GITHUB_ISSUE_SNAPSHOT_FIXTURE_JSON",
        json.dumps(_issue_payload()),
    )

    def exploding_runner(*args, **kwargs):
        raise AssertionError("gh should not be invoked when a fixture is present")

    snap = capture_github_issue_snapshot("acme/repo", "284", runner=exploding_runner)
    assert snap["source"]["snapshot_hash"]


def test_snapshot_hash_of_tolerates_missing_or_malformed():
    assert snapshot_hash_of(None) == ""
    assert snapshot_hash_of({}) == ""
    assert snapshot_hash_of({"source": {}}) == ""
    assert snapshot_hash_of({"source": {"snapshot_hash": "abc"}}) == "abc"


def test_detect_source_drift_no_snapshot_is_not_drifted():
    verdict = detect_source_drift(None, None)
    assert verdict["drifted"] is False
    assert verdict["reason_code"] == "no_snapshot"


def test_detect_source_drift_same_hash_is_unchanged():
    snap = capture_github_issue_snapshot("acme/repo", "284", runner=_fake_runner(_issue_payload()))
    verdict = detect_source_drift(snap, snap)
    assert verdict["drifted"] is False
    assert verdict["reason_code"] == "source_unchanged"


def test_detect_source_drift_different_hash_is_drifted():
    a = capture_github_issue_snapshot("acme/repo", "284", runner=_fake_runner(_issue_payload(body="v1")))
    b = capture_github_issue_snapshot("acme/repo", "284", runner=_fake_runner(_issue_payload(body="v2")))
    verdict = detect_source_drift(a, b)
    assert verdict["drifted"] is True
    assert verdict["reason_code"] == "source_changed"
    assert verdict["before"] != verdict["after"]
