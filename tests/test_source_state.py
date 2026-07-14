import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import simplicio_loop.source_state as source_state
from simplicio_loop.source_state import github_delivery_payload, infer_github_delivery_state


def _fake_completed(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# infer_github_delivery_state — pure-function unit coverage of every branch
# ---------------------------------------------------------------------------

def test_infer_state_deployed_when_smoke_passed():
    payload = {"deployment": {"environment": "prod", "smoke": {"passed": True}}}
    assert infer_github_delivery_state(payload) == "deployed"


def test_infer_state_released_when_release_and_smoke_present():
    payload = {
        "release": {"tag": "v1.0.0", "assets": ["sbom.json"]},
        "install_smoke": {"passed": True},
    }
    assert infer_github_delivery_state(payload) == "released"


def test_infer_state_merged_when_merge_fields_present():
    payload = {
        "merge": {
            "commit_sha": "abc123",
            "merged_at": "2026-07-01T00:00:00Z",
            "commit_in_default_branch": True,
        }
    }
    assert infer_github_delivery_state(payload) == "merged"


def test_infer_state_merge_ready_when_pr_checks_reviews_and_branch_all_clear():
    payload = {
        "pr": {"url": "https://example/pr/1", "head_sha": "a", "base_sha": "b"},
        "checks": {"green": True},
        "reviews": {"approvals": 1, "open_threads": 0},
        "branch": {"up_to_date": True},
    }
    assert infer_github_delivery_state(payload) == "merge-ready"


def test_infer_state_pr_open_when_pr_present_but_checks_not_green():
    payload = {
        "pr": {"url": "https://example/pr/1", "head_sha": "a", "base_sha": "b"},
        "checks": {"green": False},
        "reviews": {"approvals": 1, "open_threads": 0},
        "branch": {"up_to_date": True},
    }
    assert infer_github_delivery_state(payload) == "pr-open"


def test_infer_state_pr_open_when_review_threads_still_open():
    payload = {
        "pr": {"url": "https://example/pr/1", "head_sha": "a", "base_sha": "b"},
        "checks": {"green": True},
        "reviews": {"approvals": 1, "open_threads": 2},
        "branch": {"up_to_date": True},
    }
    assert infer_github_delivery_state(payload) == "pr-open"


def test_infer_state_defaults_to_verified_when_nothing_matches():
    assert infer_github_delivery_state({}) == "verified"


# ---------------------------------------------------------------------------
# github_delivery_payload — fixture-driven paths (no real `gh` subprocess)
# ---------------------------------------------------------------------------

def test_github_delivery_payload_uses_fixture_env_var(monkeypatch):
    fixture = {"pr": {"url": "https://example/pr/9"}}
    monkeypatch.setenv("SIMPLICIO_LOOP_GITHUB_FIXTURE_JSON", json.dumps(fixture))
    payload = github_delivery_payload("acme/widgets", pr=9, tag="", target_state="pr-open")
    assert payload["pr"]["url"] == "https://example/pr/9"
    assert payload["source_query"] == {
        "provider": "github",
        "repo": "acme/widgets",
        "pr": 9,
        "tag": "",
        "target_state": "pr-open",
        "mode": "fixture",
    }


def test_github_delivery_payload_live_mode_with_no_pr_or_tag_returns_bare_source_query(monkeypatch):
    monkeypatch.delenv("SIMPLICIO_LOOP_GITHUB_FIXTURE_JSON", raising=False)
    payload = github_delivery_payload("acme/widgets")
    assert payload == {
        "source_query": {
            "provider": "github",
            "repo": "acme/widgets",
            "pr": None,
            "tag": "",
            "target_state": "",
            "mode": "live",
        }
    }


def test_github_delivery_payload_live_mode_pr_view_failure_raises(monkeypatch):
    monkeypatch.delenv("SIMPLICIO_LOOP_GITHUB_FIXTURE_JSON", raising=False)
    monkeypatch.setattr(source_state, "_run_gh",
                        lambda args: _fake_completed(returncode=1, stderr="gh: not found"))
    try:
        github_delivery_payload("acme/widgets", pr=5)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "gh pr view failed" in str(exc)


def test_github_delivery_payload_live_mode_pr_view_builds_merge_ready_shape(monkeypatch):
    monkeypatch.delenv("SIMPLICIO_LOOP_GITHUB_FIXTURE_JSON", raising=False)
    monkeypatch.delenv("SIMPLICIO_LOOP_GITHUB_REVIEWS_FIXTURE_JSON", raising=False)
    pr_json = json.dumps({
        "url": "https://example/pr/5",
        "headRefOid": "abc",
        "baseRefOid": "def",
        "reviewDecision": "APPROVED",
        "mergeStateStatus": "CLEAN",
        "mergedAt": "",
        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
    })
    monkeypatch.setattr(source_state, "_run_gh", lambda args: _fake_completed(stdout=pr_json))
    payload = github_delivery_payload("acme/widgets", pr=5)
    assert payload["pr"]["url"] == "https://example/pr/5"
    assert payload["checks"]["green"] is True
    assert payload["reviews"]["approvals"] == 1
    assert payload["branch"]["up_to_date"] is True
    assert "merge" not in payload


def test_github_delivery_payload_live_mode_pr_view_uses_reviews_fixture_and_merged_state(monkeypatch):
    monkeypatch.delenv("SIMPLICIO_LOOP_GITHUB_FIXTURE_JSON", raising=False)
    monkeypatch.setenv("SIMPLICIO_LOOP_GITHUB_REVIEWS_FIXTURE_JSON",
                       json.dumps({"approvals": 2, "open_threads": 1}))
    pr_json = json.dumps({
        "url": "https://example/pr/6",
        "headRefOid": "abc",
        "baseRefOid": "def",
        "reviewDecision": "REVIEW_REQUIRED",
        "mergeStateStatus": "UNSTABLE",
        "mergedAt": "2026-07-01T00:00:00Z",
        "mergeCommit": {"oid": "mmm"},
        "statusCheckRollup": [],
    })
    monkeypatch.setattr(source_state, "_run_gh", lambda args: _fake_completed(stdout=pr_json))
    payload = github_delivery_payload("acme/widgets", pr=6)
    assert payload["reviews"] == {"approvals": 2, "open_threads": 1}
    assert payload["checks"]["green"] is False
    assert payload["merge"]["commit_sha"] == "mmm"
    assert payload["merge"]["commit_in_default_branch"] is True


def test_github_delivery_payload_live_mode_release_view_failure_raises(monkeypatch):
    monkeypatch.delenv("SIMPLICIO_LOOP_GITHUB_FIXTURE_JSON", raising=False)
    monkeypatch.setattr(source_state, "_run_gh",
                        lambda args: _fake_completed(returncode=1, stderr="gh: release missing"))
    try:
        github_delivery_payload("acme/widgets", tag="v1.0.0")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "gh release view failed" in str(exc)


def test_github_delivery_payload_live_mode_release_view_builds_release_shape(monkeypatch):
    monkeypatch.delenv("SIMPLICIO_LOOP_GITHUB_FIXTURE_JSON", raising=False)
    release_json = json.dumps({
        "tagName": "v1.0.0",
        "assets": [{"name": "sbom.json"}, {"name": "checksums.txt"}],
    })
    monkeypatch.setattr(source_state, "_run_gh", lambda args: _fake_completed(stdout=release_json))
    payload = github_delivery_payload("acme/widgets", tag="v1.0.0")
    assert payload["release"]["tag"] == "v1.0.0"
    assert payload["release"]["assets"] == ["sbom.json", "checksums.txt"]
    assert payload["release"]["checksums_verified"] is True
    assert payload["install_smoke"]["passed"] is True
