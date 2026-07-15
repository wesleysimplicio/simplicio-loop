"""Unit tests for the #289 short-lived-credential scheme.

This does not replace the OIDC broker described in #289 (no CI identity
provider exists in this repo to issue the initial trust) but it proves the
achievable-without-CI slice: expiry, `jti`, subject/scope/audience binding,
signature integrity, and revocation are all real and fail closed.
"""
import time

import pytest

from scripts.short_lived_credentials import (
    CredentialError,
    is_revoked,
    issue_token,
    revoke_jti,
    verify_token,
)


def test_issue_and_verify_roundtrip():
    token = issue_token("s3cret", subject="agent-1", scope="staging", ttl_seconds=60)
    claims = verify_token("s3cret", token, expected_subject="agent-1", expected_scope="staging")
    assert claims["sub"] == "agent-1"
    assert claims["scope"] == "staging"
    assert claims["jti"]
    assert claims["exp"] > time.time()


def test_two_issuances_have_distinct_jti():
    t1 = issue_token("s3cret", subject="agent-1", scope="staging", ttl_seconds=60)
    t2 = issue_token("s3cret", subject="agent-1", scope="staging", ttl_seconds=60)
    c1 = verify_token("s3cret", t1)
    c2 = verify_token("s3cret", t2)
    assert c1["jti"] != c2["jti"]


def test_expired_token_is_rejected():
    token = issue_token("s3cret", subject="agent-1", scope="staging", ttl_seconds=0.05)
    time.sleep(0.15)
    with pytest.raises(CredentialError, match="expired"):
        verify_token("s3cret", token, clock_skew_seconds=0.0)


def test_future_nbf_is_rejected():
    # issue_token always sets nbf=now (extra_claims cannot override reserved
    # claims -- see test_extra_claims_cannot_forge_reserved_fields below), so a
    # future-nbf token is built directly with the module's own signing
    # primitives, exactly like a broker that deliberately delays activation.
    import json as _json
    from scripts.short_lived_credentials import _b64u_encode, _sign, TOKEN_SCHEMA
    now = time.time()
    claims = {"schema": TOKEN_SCHEMA, "sub": "agent-1", "scope": "staging", "aud": None,
              "jti": "future-nbf-jti", "iat": now, "nbf": now + 30, "exp": now + 60}
    payload_b64 = _b64u_encode(_json.dumps(claims, sort_keys=True).encode("utf-8"))
    token = payload_b64 + "." + _b64u_encode(_sign("s3cret", payload_b64.encode("ascii")))
    with pytest.raises(CredentialError, match="not yet valid"):
        verify_token("s3cret", token, clock_skew_seconds=0.0)


def test_extra_claims_cannot_forge_reserved_fields():
    token = issue_token("s3cret", subject="agent-1", scope="staging", ttl_seconds=60,
                        extra_claims={"scope": "production", "sub": "someone-else"})
    claims = verify_token("s3cret", token)
    assert claims["scope"] == "staging"
    assert claims["sub"] == "agent-1"


def test_wrong_secret_is_rejected():
    token = issue_token("s3cret", subject="agent-1", scope="staging", ttl_seconds=60)
    with pytest.raises(CredentialError, match="invalid token signature"):
        verify_token("wrong-secret", token)


def test_tampered_payload_is_rejected():
    token = issue_token("s3cret", subject="agent-1", scope="staging", ttl_seconds=60)
    payload_b64, sig_b64 = token.split(".", 1)
    tampered = payload_b64 + "x." + sig_b64
    with pytest.raises(CredentialError):
        verify_token("s3cret", tampered)


def test_scope_mismatch_is_rejected():
    token = issue_token("s3cret", subject="agent-1", scope="staging", ttl_seconds=60)
    with pytest.raises(CredentialError, match="scope mismatch"):
        verify_token("s3cret", token, expected_scope="production")


def test_subject_mismatch_is_rejected():
    token = issue_token("s3cret", subject="agent-1", scope="staging", ttl_seconds=60)
    with pytest.raises(CredentialError, match="subject mismatch"):
        verify_token("s3cret", token, expected_subject="agent-2")


def test_audience_mismatch_is_rejected():
    token = issue_token("s3cret", subject="agent-1", scope="staging", ttl_seconds=60, audience="queue-a")
    with pytest.raises(CredentialError, match="audience mismatch"):
        verify_token("s3cret", token, expected_audience="queue-b")


def test_malformed_token_forms_are_rejected():
    for bad in ("", "no-dot-here", ".", "a.", ".b"):
        with pytest.raises(CredentialError):
            verify_token("s3cret", bad)


def test_missing_secret_fails_closed_on_issue_and_verify():
    with pytest.raises(CredentialError):
        issue_token("", subject="agent-1", scope="staging", ttl_seconds=60)
    token = issue_token("s3cret", subject="agent-1", scope="staging", ttl_seconds=60)
    with pytest.raises(CredentialError):
        verify_token("", token)


def test_non_positive_ttl_is_rejected():
    with pytest.raises(CredentialError, match="ttl_seconds must be positive"):
        issue_token("s3cret", subject="agent-1", scope="staging", ttl_seconds=0)


def test_revoked_jti_is_rejected_even_before_natural_expiry(tmp_path):
    store = tmp_path / "revoked.json"
    token = issue_token("s3cret", subject="agent-1", scope="staging", ttl_seconds=600)
    claims = verify_token("s3cret", token, revocation_store=store)  # not yet revoked
    assert claims["sub"] == "agent-1"

    revoke_jti(store, claims["jti"], ttl_seconds=3600)
    assert is_revoked(store, claims["jti"]) is True
    with pytest.raises(CredentialError, match="revoked"):
        verify_token("s3cret", token, revocation_store=store)


def test_revocation_store_prunes_expired_entries(tmp_path):
    store = tmp_path / "revoked.json"
    revoke_jti(store, "jti-expiring-fast", ttl_seconds=0.05)
    assert is_revoked(store, "jti-expiring-fast") is True
    time.sleep(0.15)
    assert is_revoked(store, "jti-expiring-fast") is False
    # a fresh revoke call should not resurrect the pruned entry under a new jti
    revoke_jti(store, "jti-2", ttl_seconds=60)
    assert is_revoked(store, "jti-expiring-fast") is False
    assert is_revoked(store, "jti-2") is True


def test_missing_jti_is_rejected(monkeypatch):
    # Simulate a forged/legacy token that omits jti by round-tripping through
    # the same signing primitives issue_token uses, but stripping the claim.
    import json as _json
    from scripts.short_lived_credentials import _b64u_decode, _b64u_encode, _sign, TOKEN_SCHEMA
    claims = {"schema": TOKEN_SCHEMA, "sub": "agent-1", "scope": "staging", "aud": None,
              "iat": time.time(), "nbf": time.time(), "exp": time.time() + 60}
    payload_b64 = _b64u_encode(_json.dumps(claims, sort_keys=True).encode("utf-8"))
    token = payload_b64 + "." + _b64u_encode(_sign("s3cret", payload_b64.encode("ascii")))
    with pytest.raises(CredentialError, match="missing jti"):
        verify_token("s3cret", token)
