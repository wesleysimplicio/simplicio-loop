#!/usr/bin/env python3
"""Short-lived, revocable bearer credentials for the distributed queue (#289).

Gap this closes: PR #320/#346 replaced a *free-form destination* with a
policy-resolved one, but the credential handed to that destination
(``SIMPLICIO_REMOTE_QUEUE_TOKEN``) was still a static, indefinitely-lived shared
secret -- no expiry, no unique identifier per issuance, no revocation. The full
fix described in #289 is an OIDC broker exchange, which needs a CI identity
provider (GitHub Actions) this repo currently does not have (removed in #311).

This module does not implement OIDC. It implements the piece of "short-lived
credentials + jti + revocation" that is achievable without CI: an HMAC-signed
token with a mandatory expiry (``exp``), a unique ``jti``, a subject/scope/
audience binding, and a fail-closed revocation store. Whatever process today
exports a static ``SIMPLICIO_REMOTE_QUEUE_TOKEN`` can instead hold a long-lived
*signing secret* (``SIMPLICIO_REMOTE_QUEUE_TOKEN_SECRET``) and mint a
short-lived token per run via :func:`issue_token`; the queue server verifies it
via :func:`verify_token`, which enforces expiry, not-before, signature, and
revocation before treating the caller as authenticated. A compromised token
stops working the moment it expires (minutes, not forever) and can be revoked
immediately via :func:`revoke_jti` without rotating the underlying secret.

Token shape (not a JWT -- no external dependency, no algorithm-confusion attack
surface): ``base64url(payload-json) + "." + base64url(hmac_sha256(payload))``.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

TOKEN_SCHEMA = "simplicio.short-lived-credential/v1"
REVOCATION_STORE_SCHEMA = "simplicio.revocation-store/v1"
DEFAULT_REVOCATION_STORE = Path(".orchestrator") / "security" / "revoked-jti.json"
DEFAULT_TTL_SECONDS = 300.0
DEFAULT_CLOCK_SKEW_SECONDS = 5.0


class CredentialError(ValueError):
    """Raised for any issue/verify/revoke failure. Callers must fail closed."""


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + padding)
    except Exception as exc:  # noqa: BLE001 - normalize to CredentialError
        raise CredentialError(f"malformed base64url segment: {exc}") from exc


def _sign(secret: str, message: bytes) -> bytes:
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()


def issue_token(
    secret: str,
    *,
    subject: str,
    scope: str,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    audience: Optional[str] = None,
    extra_claims: Optional[Dict[str, Any]] = None,
) -> str:
    """Mint a short-lived, HMAC-signed token bound to ``subject``/``scope``.

    Fails closed on a missing secret or non-positive TTL rather than issuing an
    unbounded credential.
    """
    if not secret:
        raise CredentialError("a non-empty signing secret is required")
    if not subject:
        raise CredentialError("subject is required")
    if not scope:
        raise CredentialError("scope is required")
    if ttl_seconds <= 0:
        raise CredentialError("ttl_seconds must be positive")
    now = time.time()
    claims: Dict[str, Any] = {
        "schema": TOKEN_SCHEMA,
        "sub": subject,
        "scope": scope,
        "aud": audience,
        "jti": secrets.token_hex(16),
        "iat": now,
        "nbf": now,
        "exp": now + float(ttl_seconds),
    }
    if extra_claims:
        for key in ("schema", "sub", "scope", "aud", "jti", "iat", "nbf", "exp"):
            extra_claims.pop(key, None)
        claims.update(extra_claims)
    payload_b64 = _b64u_encode(json.dumps(claims, sort_keys=True).encode("utf-8"))
    signature = _sign(secret, payload_b64.encode("ascii"))
    return payload_b64 + "." + _b64u_encode(signature)


def _read_store(path: Path) -> Dict[str, float]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise CredentialError(f"revocation store is unreadable: {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("revoked"), dict):
        return {}
    out: Dict[str, float] = {}
    for jti, revoked_until in data["revoked"].items():
        try:
            out[str(jti)] = float(revoked_until)
        except (TypeError, ValueError):
            continue
    return out


def is_revoked(path: Path, jti: str) -> bool:
    if not jti:
        return True
    store = _read_store(path)
    revoked_until = store.get(jti)
    return revoked_until is not None and revoked_until > time.time()


def revoke_jti(path: Path, jti: str, *, ttl_seconds: float = 86400.0) -> None:
    """Mark ``jti`` as revoked until ``ttl_seconds`` from now.

    Entries past their own expiry are pruned on every write so the store does
    not grow without bound; a revoked jti stays rejected even after the token
    it belonged to would otherwise have expired naturally, closing the replay
    window an attacker could otherwise exploit right up to ``exp``.
    """
    if not jti:
        raise CredentialError("jti is required to revoke a credential")
    path.parent.mkdir(parents=True, exist_ok=True)
    store = _read_store(path)
    now = time.time()
    store = {k: v for k, v in store.items() if v > now}
    store[jti] = now + max(ttl_seconds, 0.0)
    path.write_text(
        json.dumps({"schema": REVOCATION_STORE_SCHEMA, "revoked": store}, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def verify_token(
    secret: str,
    token: str,
    *,
    expected_scope: Optional[str] = None,
    expected_subject: Optional[str] = None,
    expected_audience: Optional[str] = None,
    revocation_store: Optional[Path] = None,
    clock_skew_seconds: float = DEFAULT_CLOCK_SKEW_SECONDS,
) -> Dict[str, Any]:
    """Verify signature, expiry, nbf, binding and revocation. Raises on any failure.

    Never returns a partially-trusted result: every check either passes or this
    raises :class:`CredentialError`, so callers can treat a returned ``dict`` as
    fully authenticated claims.
    """
    if not secret:
        raise CredentialError("a non-empty verification secret is required")
    if not token or "." not in token:
        raise CredentialError("malformed token")
    payload_b64, _, sig_b64 = token.partition(".")
    if not payload_b64 or not sig_b64:
        raise CredentialError("malformed token")
    expected_sig = _sign(secret, payload_b64.encode("ascii"))
    actual_sig = _b64u_decode(sig_b64)
    if not hmac.compare_digest(expected_sig, actual_sig):
        raise CredentialError("invalid token signature")
    try:
        claims = json.loads(_b64u_decode(payload_b64).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CredentialError(f"malformed token payload: {exc}") from exc
    if not isinstance(claims, dict):
        raise CredentialError("token payload must be an object")
    if claims.get("schema") != TOKEN_SCHEMA:
        raise CredentialError("unexpected token schema")
    now = time.time()
    try:
        exp = float(claims.get("exp", 0))
        nbf = float(claims.get("nbf", 0))
    except (TypeError, ValueError) as exc:
        raise CredentialError(f"malformed exp/nbf claim: {exc}") from exc
    if now > exp + clock_skew_seconds:
        raise CredentialError("token expired")
    if now < nbf - clock_skew_seconds:
        raise CredentialError("token not yet valid (nbf in the future)")
    if expected_scope is not None and claims.get("scope") != expected_scope:
        raise CredentialError("token scope mismatch")
    if expected_subject is not None and claims.get("sub") != expected_subject:
        raise CredentialError("token subject mismatch")
    if expected_audience is not None and claims.get("aud") != expected_audience:
        raise CredentialError("token audience mismatch")
    jti = claims.get("jti")
    if not jti or not isinstance(jti, str):
        raise CredentialError("token is missing jti")
    if revocation_store is not None and is_revoked(revocation_store, jti):
        raise CredentialError("token has been revoked")
    return claims


def _cmd_issue(args: argparse.Namespace) -> int:
    try:
        token = issue_token(
            args.secret, subject=args.subject, scope=args.scope,
            ttl_seconds=args.ttl_seconds, audience=args.audience,
        )
    except CredentialError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, "token": token}))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    store = Path(args.revocation_store) if args.revocation_store else None
    try:
        claims = verify_token(
            args.secret, args.token, expected_scope=args.scope,
            expected_subject=args.subject, expected_audience=args.audience,
            revocation_store=store,
        )
    except CredentialError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, "claims": {k: v for k, v in claims.items() if k != "schema"}}))
    return 0


def _cmd_revoke(args: argparse.Namespace) -> int:
    store = Path(args.revocation_store)
    try:
        revoke_jti(store, args.jti, ttl_seconds=args.ttl_seconds)
    except CredentialError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True}))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_issue = sub.add_parser("issue")
    p_issue.add_argument("--secret", required=True)
    p_issue.add_argument("--subject", required=True)
    p_issue.add_argument("--scope", required=True)
    p_issue.add_argument("--audience", default=None)
    p_issue.add_argument("--ttl-seconds", type=float, default=DEFAULT_TTL_SECONDS)
    p_issue.set_defaults(func=_cmd_issue)

    p_verify = sub.add_parser("verify")
    p_verify.add_argument("--secret", required=True)
    p_verify.add_argument("--token", required=True)
    p_verify.add_argument("--subject", default=None)
    p_verify.add_argument("--scope", default=None)
    p_verify.add_argument("--audience", default=None)
    p_verify.add_argument("--revocation-store", default=str(DEFAULT_REVOCATION_STORE))
    p_verify.set_defaults(func=_cmd_verify)

    p_revoke = sub.add_parser("revoke")
    p_revoke.add_argument("--jti", required=True)
    p_revoke.add_argument("--revocation-store", default=str(DEFAULT_REVOCATION_STORE))
    p_revoke.add_argument("--ttl-seconds", type=float, default=86400.0)
    p_revoke.set_defaults(func=_cmd_revoke)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
