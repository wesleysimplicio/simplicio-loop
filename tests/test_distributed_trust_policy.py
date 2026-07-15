import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from scripts.distributed_trust_policy import (
    TrustPolicyError,
    authorize,
    check_endpoint,
    load_policy,
    resolve_environment,
    validate_schema,
)


def _base_policy():
    return {
        "schema": "simplicio.distributed-trust-policy/v1",
        "environments": {
            "staging": {
                "description": "test",
                "origin": {
                    "scheme": "https",
                    "hostname": "queue.example.internal",
                    "port": 443,
                    "base_path": "/",
                },
                "tls_sha256_pins": ["aa" * 32],
                "oidc_audience": "aud",
                "github_environment": "distributed-staging",
                "allowed_repos": ["acme/widgets"],
                "allowed_refs": ["refs/heads/main"],
                "allowed_actors": [],
                "max_ttl_seconds": 900,
                "egress": {"allow_redirects": False, "allow_proxy_env": False},
                "contacts": ["sec@example.com"],
                "reviewed_at": "2026-07-14",
                "revocation_procedure": "rotate",
            }
        },
    }


# ---------------------------------------------------------------------------
# schema validation
# ---------------------------------------------------------------------------

def test_real_policy_file_on_disk_validates():
    # Exercises the actual committed policy, not just a synthetic fixture.
    policy = load_policy()
    assert "staging" in policy["environments"]


def test_validate_schema_rejects_wrong_schema_tag():
    policy = _base_policy()
    policy["schema"] = "something-else"
    with pytest.raises(TrustPolicyError):
        validate_schema(policy)


def test_validate_schema_rejects_missing_environments():
    with pytest.raises(TrustPolicyError):
        validate_schema({"schema": "simplicio.distributed-trust-policy/v1", "environments": {}})


def test_validate_schema_rejects_missing_required_field():
    policy = _base_policy()
    del policy["environments"]["staging"]["tls_sha256_pins"]
    with pytest.raises(TrustPolicyError):
        validate_schema(policy)


def test_validate_schema_rejects_non_https_scheme():
    policy = _base_policy()
    policy["environments"]["staging"]["origin"]["scheme"] = "http"
    with pytest.raises(TrustPolicyError):
        validate_schema(policy)


# ---------------------------------------------------------------------------
# resolve_environment — enumerated environment_id only, never free text
# ---------------------------------------------------------------------------

def test_resolve_environment_returns_policy_values():
    policy = _base_policy()
    env = resolve_environment(policy, "staging")
    assert env["origin"]["hostname"] == "queue.example.internal"


def test_resolve_environment_rejects_unknown_environment_id():
    policy = _base_policy()
    with pytest.raises(TrustPolicyError):
        resolve_environment(policy, "attacker-controlled")


# ---------------------------------------------------------------------------
# authorize — repo/ref/actor must be in the policy allow-list
# ---------------------------------------------------------------------------

def test_authorize_passes_for_allowed_repo_and_ref():
    policy = _base_policy()
    ok, reason = authorize(policy, "staging", "acme/widgets", "refs/heads/main", "anyone")
    assert ok, reason


def test_authorize_fails_for_unauthorized_repo():
    policy = _base_policy()
    ok, reason = authorize(policy, "staging", "attacker/fork", "refs/heads/main", "anyone")
    assert not ok
    assert "repo" in reason


def test_authorize_fails_for_unauthorized_ref():
    policy = _base_policy()
    ok, reason = authorize(policy, "staging", "acme/widgets", "refs/heads/attacker-branch", "anyone")
    assert not ok
    assert "ref" in reason


def test_authorize_enforces_actor_allowlist_when_present():
    policy = _base_policy()
    policy["environments"]["staging"]["allowed_actors"] = ["trusted-bot"]
    ok, reason = authorize(policy, "staging", "acme/widgets", "refs/heads/main", "someone-else")
    assert not ok
    assert "actor" in reason


def test_authorize_fails_closed_for_unknown_environment():
    policy = _base_policy()
    ok, reason = authorize(policy, "does-not-exist", "acme/widgets", "refs/heads/main", "anyone")
    assert not ok


# ---------------------------------------------------------------------------
# check-endpoint — the core exfiltration-prevention gate (#289)
# ---------------------------------------------------------------------------

def test_check_endpoint_passes_when_operational_values_match_policy():
    policy = _base_policy()
    ok, reason = check_endpoint(
        policy, "staging", "https://queue.example.internal:443/", "queue.example.internal", "aa" * 32
    )
    assert ok, reason


def test_check_endpoint_fails_closed_when_hostname_is_attacker_controlled():
    # This is the exact exploit the issue describes: an attacker-chosen hostname with a
    # matching self-supplied fingerprint must never be trusted.
    policy = _base_policy()
    ok, reason = check_endpoint(
        policy, "staging", "https://attacker.example:443/", "attacker.example", "aa" * 32
    )
    assert not ok
    assert "origin" in reason


def test_check_endpoint_fails_closed_on_fingerprint_mismatch():
    policy = _base_policy()
    ok, reason = check_endpoint(
        policy, "staging", "https://queue.example.internal:443/", "queue.example.internal", "bb" * 32
    )
    assert not ok
    assert "tls_sha256" in reason


def test_check_endpoint_rejects_non_https_scheme():
    policy = _base_policy()
    ok, reason = check_endpoint(
        policy, "staging", "http://queue.example.internal:443/", "queue.example.internal", "aa" * 32
    )
    assert not ok


def test_check_endpoint_rejects_userinfo_in_url():
    policy = _base_policy()
    ok, reason = check_endpoint(
        policy,
        "staging",
        "https://attacker:secret@queue.example.internal:443/",
        "queue.example.internal",
        "aa" * 32,
    )
    assert not ok
    assert "userinfo" in reason


def test_check_endpoint_fails_closed_for_unknown_environment():
    policy = _base_policy()
    ok, reason = check_endpoint(
        policy, "unknown-env", "https://queue.example.internal:443/", "queue.example.internal", "aa" * 32
    )
    assert not ok
