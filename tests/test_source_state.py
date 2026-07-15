import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import simplicio_loop.source_state as source_state
from simplicio_loop.source_state import (
    github_delivery_payload,
    infer_github_delivery_state,
    query_review_threads,
)


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
        "release": {
            "tag": "v1.0.0",
            "assets": ["sbom.json"],
            "checksums_verified": True,
            "signatures_verified": True,
            "sbom_present": True,
        },
        "install_smoke": {"passed": True},
    }
    assert infer_github_delivery_state(payload) == "released"


def test_infer_state_not_released_when_release_present_but_unverified():
    # #290 — presence of a tag/assets is not proof the bytes were checked; without an explicit
    # verified marker on checksums/signatures/sbom the state must not promote to "released".
    payload = {
        "release": {"tag": "v1.0.0", "assets": ["sbom.json"]},
        "install_smoke": {"passed": True},
    }
    assert infer_github_delivery_state(payload) == "verified"


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
        "reviews": {"approvals": 1, "open_threads": 0, "open_threads_verified": True},
        "branch": {"up_to_date": True},
    }
    assert infer_github_delivery_state(payload) == "merge-ready"


def test_infer_state_pr_open_when_open_threads_count_present_but_unqueried():
    # #290 — `open_threads: 0` alone must never mean "verified clear"; without a paginated
    # review-thread query having actually run (`open_threads_verified`), this must stay pr-open.
    payload = {
        "pr": {"url": "https://example/pr/1", "head_sha": "a", "base_sha": "b"},
        "checks": {"green": True},
        "reviews": {"approvals": 1, "open_threads": 0},
        "branch": {"up_to_date": True},
    }
    assert infer_github_delivery_state(payload) == "pr-open"


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
    # #290 — no reviews fixture was set, so the open-threads count was never actually queried.
    assert payload["reviews"]["open_threads_verified"] is False
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
    assert payload["reviews"] == {
        "approvals": 2, "open_threads": 1, "open_threads_verified": True,
        "trust_level": "test-fixture",
    }
    assert payload["checks"]["green"] is False
    # #290 — UNSTABLE must not be folded into "up to date": only CLEAN proves it.
    assert payload["branch"]["up_to_date"] is False
    assert payload["merge"]["commit_sha"] == "mmm"
    # #290 — a merged PR event does not by itself prove the commit is reachable from the real
    # default branch; no BranchReachabilityVerifier ran, so this must fail closed.
    assert payload["merge"]["commit_in_default_branch"] is False
    assert payload["merge"]["commit_in_default_branch_reason_code"] == "merge_reachability_unverified"


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
    # #290 — `gh release view` only proves a tag/asset list exists, not that the bytes were
    # downloaded and checked. No verifier ran, so these must fail closed instead of asserting a
    # convenient `true`.
    assert payload["release"]["checksums_verified"] is False
    assert payload["release"]["signatures_verified"] is False
    assert payload["release"]["sbom_present"] is False
    assert payload["install_smoke"]["passed"] is False


# ---------------------------------------------------------------------------
# query_review_threads — real paginated GraphQL review-thread query (#290 Fase 2.4)
# ---------------------------------------------------------------------------

def _graphql_page(nodes, has_next_page, end_cursor=None, approved_reviews=1):
    return json.dumps({
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "totalCount": len(nodes),
                        "pageInfo": {"hasNextPage": has_next_page, "endCursor": end_cursor},
                        "nodes": nodes,
                    },
                    "reviews": {"totalCount": approved_reviews},
                }
            }
        }
    })


def test_query_review_threads_single_page_all_resolved_is_verified(monkeypatch):
    page = _graphql_page([{"isResolved": True}, {"isResolved": True}], has_next_page=False, approved_reviews=2)
    monkeypatch.setattr(source_state, "_run_gh", lambda args: _fake_completed(stdout=page))
    result = query_review_threads("acme", "widgets", 5)
    assert result["open_threads"] == 0
    assert result["open_threads_verified"] is True
    assert result["approvals"] == 2
    assert result["pages"] == 1
    assert result["reason_code"] is None


def test_query_review_threads_unresolved_thread_on_second_page_blocks(monkeypatch):
    # #290 — "thread aberta apenas na segunda página bloqueia": an unresolved thread that only
    # shows up on page 2 must still be counted; pagination must not stop early.
    calls = {"n": 0}

    def _fake_run(args):
        calls["n"] += 1
        if calls["n"] == 1:
            return _fake_completed(stdout=_graphql_page([{"isResolved": True}], has_next_page=True, end_cursor="c1"))
        return _fake_completed(stdout=_graphql_page([{"isResolved": False}], has_next_page=False))

    monkeypatch.setattr(source_state, "_run_gh", _fake_run)
    result = query_review_threads("acme", "widgets", 5)
    assert result["pages"] == 2
    assert result["open_threads"] == 1
    assert result["open_threads_verified"] is True
    assert result["total_threads"] == 2


def test_query_review_threads_gh_failure_is_unverified_with_reason_code(monkeypatch):
    monkeypatch.setattr(source_state, "_run_gh", lambda args: _fake_completed(returncode=1, stderr="rate limited"))
    result = query_review_threads("acme", "widgets", 5)
    assert result["open_threads_verified"] is False
    assert result["reason_code"] == "review_threads_query_failed"


def test_query_review_threads_malformed_response_is_unverified(monkeypatch):
    monkeypatch.setattr(source_state, "_run_gh", lambda args: _fake_completed(stdout="{}"))
    result = query_review_threads("acme", "widgets", 5)
    assert result["open_threads_verified"] is False
    assert result["reason_code"] == "review_threads_response_malformed"


def test_query_review_threads_incomplete_pagination_never_verifies(monkeypatch):
    # #290 "Complete pagination" invariant: if hasNextPage stays true past the page budget,
    # this must report `pagination_incomplete`, never a favorable verified-clear default.
    page = _graphql_page([{"isResolved": True}], has_next_page=True, end_cursor="next")
    monkeypatch.setattr(source_state, "_run_gh", lambda args: _fake_completed(stdout=page))
    result = query_review_threads("acme", "widgets", 5, max_pages=3)
    assert result["pages"] == 3
    assert result["open_threads_verified"] is False
    assert result["reason_code"] == "pagination_incomplete"


def test_github_delivery_payload_live_mode_real_pagination_populates_reviews(monkeypatch):
    monkeypatch.delenv("SIMPLICIO_LOOP_GITHUB_FIXTURE_JSON", raising=False)
    monkeypatch.delenv("SIMPLICIO_LOOP_GITHUB_REVIEWS_FIXTURE_JSON", raising=False)
    pr_json = json.dumps({
        "url": "https://example/pr/7",
        "headRefOid": "abc",
        "baseRefOid": "def",
        "reviewDecision": "APPROVED",
        "mergeStateStatus": "CLEAN",
        "mergedAt": "",
        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
    })
    threads_page = _graphql_page([{"isResolved": True}], has_next_page=False, approved_reviews=1)

    def _fake_run(args):
        if args[:2] == ["pr", "view"]:
            return _fake_completed(stdout=pr_json)
        return _fake_completed(stdout=threads_page)

    monkeypatch.setattr(source_state, "_run_gh", _fake_run)
    payload = github_delivery_payload("acme/widgets", pr=7)
    assert payload["reviews"]["open_threads"] == 0
    assert payload["reviews"]["open_threads_verified"] is True
    assert payload["reviews"]["approvals"] == 1
    assert payload["reviews"]["evidence"] == "github-graphql-review-threads"
    assert infer_github_delivery_state(payload) == "merge-ready"


# ---------------------------------------------------------------------------
# github_delivery_payload — byte-level release verification wiring (#290 Fase 4)
# ---------------------------------------------------------------------------

def test_should_verify_release_artifacts_defaults_to_target_state(monkeypatch):
    monkeypatch.delenv("SIMPLICIO_LOOP_VERIFY_RELEASE_ARTIFACTS", raising=False)
    assert source_state._should_verify_release_artifacts("released", None) is True
    assert source_state._should_verify_release_artifacts("deployed", None) is True
    assert source_state._should_verify_release_artifacts("pr-open", None) is False


def test_should_verify_release_artifacts_explicit_arg_wins(monkeypatch):
    monkeypatch.delenv("SIMPLICIO_LOOP_VERIFY_RELEASE_ARTIFACTS", raising=False)
    assert source_state._should_verify_release_artifacts("pr-open", True) is True
    assert source_state._should_verify_release_artifacts("released", False) is False


def test_should_verify_release_artifacts_env_override(monkeypatch):
    monkeypatch.setenv("SIMPLICIO_LOOP_VERIFY_RELEASE_ARTIFACTS", "1")
    assert source_state._should_verify_release_artifacts("pr-open", None) is True
    monkeypatch.setenv("SIMPLICIO_LOOP_VERIFY_RELEASE_ARTIFACTS", "0")
    assert source_state._should_verify_release_artifacts("released", None) is False


def test_github_delivery_payload_release_target_calls_real_verifier(monkeypatch):
    # #290 Fase 4 — when target_state is "released", the payload must call the byte-level
    # verifier (mocked here) instead of leaving hardcoded False; a passing verifier result must
    # flow through into the payload and let infer_github_delivery_state promote to "released".
    monkeypatch.delenv("SIMPLICIO_LOOP_FIXTURE_JSON", raising=False)
    monkeypatch.delenv("SIMPLICIO_LOOP_GITHUB_FIXTURE_JSON", raising=False)
    monkeypatch.delenv("SIMPLICIO_LOOP_VERIFY_RELEASE_ARTIFACTS", raising=False)
    release_json = json.dumps({
        "tagName": "v1.0.0",
        "assets": [{"name": "pkg-1.0.0-py3-none-any.whl"}, {"name": "checksums.txt"}],
    })
    monkeypatch.setattr(source_state, "_run_gh", lambda args: _fake_completed(stdout=release_json))

    def _fake_verify_release(repo, tag, asset_names, module_name="simplicio_loop"):
        return {
            "checksums_verified": True, "signatures_verified": True, "sbom_present": True,
            "digests": {"pkg-1.0.0-py3-none-any.whl": "a" * 64},
            "assets_verified": ["pkg-1.0.0-py3-none-any.whl"],
            "install_smoke": {"passed": True, "reason_code": None},
        }

    import simplicio_loop.external_verifiers as external_verifiers
    monkeypatch.setattr(external_verifiers, "verify_release", _fake_verify_release)
    payload = github_delivery_payload("acme/widgets", tag="v1.0.0", target_state="released")
    assert payload["release"]["checksums_verified"] is True
    assert payload["release"]["signatures_verified"] is True
    assert payload["release"]["sbom_present"] is True
    assert payload["release"]["evidence"] == "external-verifiers-byte-level"
    assert payload["install_smoke"]["passed"] is True
    assert infer_github_delivery_state(payload) == "released"


def test_github_delivery_payload_release_target_verifier_failure_stays_unverified(monkeypatch):
    monkeypatch.delenv("SIMPLICIO_LOOP_GITHUB_FIXTURE_JSON", raising=False)
    monkeypatch.delenv("SIMPLICIO_LOOP_VERIFY_RELEASE_ARTIFACTS", raising=False)
    release_json = json.dumps({
        "tagName": "v1.0.0",
        "assets": [{"name": "pkg-1.0.0-py3-none-any.whl"}],
    })
    monkeypatch.setattr(source_state, "_run_gh", lambda args: _fake_completed(stdout=release_json))

    def _fake_verify_release(repo, tag, asset_names, module_name="simplicio_loop"):
        return {
            "checksums_verified": False, "signatures_verified": False, "sbom_present": False,
            "checksum_reason_code": "checksum_manifest_absent",
            "signature_reason_code": "attestation_not_found",
            "sbom_reason_code": "sbom_asset_absent",
            "digests": {"pkg-1.0.0-py3-none-any.whl": "a" * 64},
            "assets_verified": [],
            "install_smoke": {"passed": False, "reason_code": "wheel_not_verified"},
        }

    import simplicio_loop.external_verifiers as external_verifiers
    monkeypatch.setattr(external_verifiers, "verify_release", _fake_verify_release)
    payload = github_delivery_payload("acme/widgets", tag="v1.0.0", target_state="released")
    assert payload["release"]["checksums_verified"] is False
    assert payload["release"]["reason_codes"]["checksum_reason_code"] == "checksum_manifest_absent"
    assert infer_github_delivery_state(payload) == "verified"
