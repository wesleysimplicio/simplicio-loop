#!/usr/bin/env python3
"""Fail-closed trust-policy resolver for the issue-183 distributed proof (#289).

Problem this closes: `.github/workflows/distributed-183-proof.yml` used to accept
`queue_url`, `tls_hostname` and `tls_sha256` as free-text `workflow_dispatch` inputs and
then handed a real bearer secret (`SIMPLICIO_REMOTE_QUEUE_TOKEN`) to whatever endpoint the
caller named. Anyone able to dispatch the workflow could point it at infrastructure they
control and receive the token (direct exfiltration), or a less-trusted actor could induce
the runner to talk to an unintended endpoint (confused deputy).

This module is the single place that resolves an *enumerated* `environment_id` to the
connection details the workflow is allowed to use. Nothing here is derived from
`workflow_dispatch` input text — the caller may only choose which pre-approved
environment_id to run against; the origin, port, TLS pins, allowed repos/refs/actors and
OIDC audience all come from the versioned, CODEOWNERS-reviewed policy file
`.github/security/distributed-trust-policy.json`.

Fail-closed rules enforced here:
  * an unknown `environment_id` is rejected, never silently defaulted;
  * repo/ref/actor must appear in the environment's allow-lists (empty allow-list for
    actors means "no additional actor restriction beyond repo/ref", not "anyone");
  * `check-endpoint` compares an *operationally configured* origin/hostname/fingerprint
    (e.g. from a GitHub Environment variable/secret) against the policy's canonical values
    and fails closed on any mismatch — the policy is always the anchor, never the input.

This intentionally does not implement the full OIDC broker exchange, job separation or
DNS/redirect hardening described in #289 — it is the scoped, fail-closed input-selection
gate the rest of that issue builds on.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urlsplit

try:  # pragma: no cover - script executed directly vs imported as package
    from scripts.security_audit_log import append_event as _audit_append
except ImportError:  # pragma: no cover
    try:
        from security_audit_log import append_event as _audit_append  # type: ignore
    except ImportError:  # pragma: no cover
        _audit_append = None

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = REPO_ROOT / ".github" / "security" / "distributed-trust-policy.json"

REQUIRED_ENV_FIELDS = {
    "description",
    "origin",
    "tls_sha256_pins",
    "oidc_audience",
    "github_environment",
    "allowed_repos",
    "allowed_refs",
    "allowed_actors",
    "max_ttl_seconds",
    "egress",
    "contacts",
    "reviewed_at",
    "revocation_procedure",
}
REQUIRED_ORIGIN_FIELDS = {"scheme", "hostname", "port", "base_path"}


class TrustPolicyError(ValueError):
    """Raised for any policy load/schema/authorization failure. Callers must fail closed."""


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> Dict[str, Any]:
    if not path.exists():
        raise TrustPolicyError(f"trust policy not found: {path}")
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TrustPolicyError(f"trust policy is not valid JSON: {exc}") from exc
    validate_schema(policy)
    return policy


def validate_schema(policy: Dict[str, Any]) -> None:
    if policy.get("schema") != "simplicio.distributed-trust-policy/v1":
        raise TrustPolicyError("unexpected or missing policy schema tag")
    environments = policy.get("environments")
    if not isinstance(environments, dict) or not environments:
        raise TrustPolicyError("policy must declare at least one environment")
    for env_id, env in environments.items():
        if not isinstance(env, dict):
            raise TrustPolicyError(f"environment '{env_id}' must be an object")
        missing = REQUIRED_ENV_FIELDS - set(env.keys())
        if missing:
            raise TrustPolicyError(f"environment '{env_id}' missing fields: {sorted(missing)}")
        origin = env.get("origin")
        if not isinstance(origin, dict):
            raise TrustPolicyError(f"environment '{env_id}' origin must be an object")
        missing_origin = REQUIRED_ORIGIN_FIELDS - set(origin.keys())
        if missing_origin:
            raise TrustPolicyError(
                f"environment '{env_id}' origin missing fields: {sorted(missing_origin)}"
            )
        if origin.get("scheme") != "https":
            raise TrustPolicyError(f"environment '{env_id}' origin scheme must be https")
        pin_entries = _validate_pins(env_id, env.get("tls_sha256_pins"))
        if not any(entry["status"] == "current" for entry in pin_entries):
            raise TrustPolicyError(
                f"environment '{env_id}' tls_sha256_pins must include at least one 'current' pin"
            )
        for list_field in ("allowed_repos", "allowed_refs", "allowed_actors"):
            if not isinstance(env.get(list_field), list):
                raise TrustPolicyError(f"environment '{env_id}' {list_field} must be a list")


_ALLOWED_PIN_STATUSES = {"current", "next", "retired"}


def _validate_pins(env_id: str, pins: Any) -> List[Dict[str, str]]:
    """Normalize ``tls_sha256_pins`` to ``[{"sha256": ..., "status": ...}]``.

    #289 pin rotation: each entry may be a bare string (legacy shape, treated
    as an implicit ``"current"`` pin so existing policy files keep validating
    unchanged) or an object ``{"sha256": "...", "status": "current"|"next"}``.
    Declaring a ``"next"`` pin alongside the ``"current"`` one lets a cert
    rotation happen by first adding the new pin as ``next`` (deployed, but not
    yet the primary), rotating the certificate, then promoting it to
    ``current`` and retiring the old one -- without a single commit that must
    change the policy and the certificate at the same instant.
    """
    if not isinstance(pins, list) or not pins:
        raise TrustPolicyError(f"environment '{env_id}' tls_sha256_pins must be a non-empty list")
    out: List[Dict[str, str]] = []
    for entry in pins:
        if isinstance(entry, str):
            if not entry:
                raise TrustPolicyError(f"environment '{env_id}' tls_sha256_pins entries must be non-empty")
            out.append({"sha256": entry, "status": "current"})
            continue
        if isinstance(entry, dict):
            sha256 = entry.get("sha256")
            status = entry.get("status", "current")
            if not isinstance(sha256, str) or not sha256:
                raise TrustPolicyError(f"environment '{env_id}' pin entry missing non-empty 'sha256'")
            if status not in _ALLOWED_PIN_STATUSES:
                raise TrustPolicyError(
                    f"environment '{env_id}' pin status '{status}' must be one of {sorted(_ALLOWED_PIN_STATUSES)}"
                )
            out.append({"sha256": sha256, "status": status})
            continue
        raise TrustPolicyError(f"environment '{env_id}' tls_sha256_pins entries must be strings or objects")
    return out


def resolve_environment(policy: Dict[str, Any], environment_id: str) -> Dict[str, Any]:
    environments = policy.get("environments", {})
    if environment_id not in environments:
        raise TrustPolicyError(
            f"unknown environment_id '{environment_id}': not present in trust policy"
        )
    return environments[environment_id]


def authorize(
    policy: Dict[str, Any], environment_id: str, repo: str, ref: str, actor: str,
    *, audit_log_path: Any = None,
) -> Tuple[bool, str]:
    def _decide(ok: bool, reason: str) -> Tuple[bool, str]:
        if _audit_append is not None:
            _audit_append(
                audit_log_path, event="distributed_trust_policy.authorize",
                decision="accept" if ok else "reject", who=actor, operation=environment_id,
                reason=reason, extra={"repo": repo, "ref": ref},
            )
        return ok, reason

    try:
        env = resolve_environment(policy, environment_id)
    except TrustPolicyError as exc:
        return _decide(False, str(exc))
    allowed_repos = env.get("allowed_repos") or []
    allowed_refs = env.get("allowed_refs") or []
    allowed_actors = env.get("allowed_actors") or []
    if repo not in allowed_repos:
        return _decide(False, f"repo '{repo}' is not authorized for environment '{environment_id}'")
    if ref not in allowed_refs:
        return _decide(False, f"ref '{ref}' is not authorized for environment '{environment_id}'")
    if allowed_actors and actor not in allowed_actors:
        return _decide(False, f"actor '{actor}' is not authorized for environment '{environment_id}'")
    return _decide(True, "authorized")


def _normalized_origin(scheme: str, hostname: str, port: int) -> str:
    return f"{scheme}://{hostname.lower().rstrip('.')}:{port}"


def check_endpoint(
    policy: Dict[str, Any],
    environment_id: str,
    queue_url: str,
    tls_hostname: str,
    tls_sha256: str,
    *, audit_log_path: Any = None,
) -> Tuple[bool, str]:
    """Fail closed unless the operationally-configured endpoint matches the policy exactly.

    The policy is always the anchor: this never derives trust from `queue_url` /
    `tls_hostname` / `tls_sha256` themselves, it only checks whether an out-of-band value
    (e.g. a GitHub Environment variable) still matches what the reviewed policy expects.

    Every call -- accepted or rejected -- appends a #289 audit-log line
    recording the environment id, resolved origin and which pin id (never the
    full fingerprint verbatim is withheld; the pin itself is not secret, but
    the log still ties the decision to a specific pin/status) matched, so an
    incident responder can see which pin authorized (or failed to authorize)
    a given connection without re-deriving it from code.
    """
    def _decide(ok: bool, reason: str, *, pin_status: str = "") -> Tuple[bool, str]:
        if _audit_append is not None:
            _audit_append(
                audit_log_path, event="distributed_trust_policy.check_endpoint",
                decision="accept" if ok else "reject", operation=environment_id,
                reason=reason, extra={"tls_hostname": tls_hostname, "pin_status": pin_status},
            )
        return ok, reason

    try:
        env = resolve_environment(policy, environment_id)
    except TrustPolicyError as exc:
        return _decide(False, str(exc))
    origin_cfg = env["origin"]
    expected_origin = _normalized_origin(origin_cfg["scheme"], origin_cfg["hostname"], int(origin_cfg["port"]))

    parsed = urlsplit(queue_url or "")
    if parsed.scheme != "https" or not parsed.hostname:
        return _decide(False, "queue_url must be an https URL with a hostname")
    if parsed.username or parsed.password:
        return _decide(False, "queue_url must not carry userinfo")
    actual_port = parsed.port or 443
    actual_origin = _normalized_origin(parsed.scheme, parsed.hostname, actual_port)
    if actual_origin != expected_origin:
        return _decide(False, f"queue_url origin '{actual_origin}' does not match policy origin '{expected_origin}'")

    if tls_hostname.lower().rstrip(".") != origin_cfg["hostname"].lower().rstrip("."):
        return _decide(False, "tls_hostname does not match policy hostname")

    normalized_fp = (tls_sha256 or "").replace(":", "").lower()
    pin_entries = _validate_pins(environment_id, env["tls_sha256_pins"])
    active_pins = {e["sha256"].replace(":", "").lower(): e["status"] for e in pin_entries if e["status"] != "retired"}
    if normalized_fp not in active_pins:
        return _decide(False, "tls_sha256 does not match any active pin in the policy")

    return _decide(True, "endpoint matches trust policy", pin_status=active_pins[normalized_fp])


def _cmd_validate_schema(args: argparse.Namespace) -> int:
    try:
        load_policy(Path(args.policy))
    except TrustPolicyError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True}))
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    try:
        policy = load_policy(Path(args.policy))
        env = resolve_environment(policy, args.environment_id)
    except TrustPolicyError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    origin = env["origin"]
    result = {
        "ok": True,
        "environment_id": args.environment_id,
        "queue_url": f"{origin['scheme']}://{origin['hostname']}:{origin['port']}{origin.get('base_path', '/')}",
        "tls_hostname": origin["hostname"],
        "tls_sha256_pins": env["tls_sha256_pins"],
        "oidc_audience": env["oidc_audience"],
        "github_environment": env["github_environment"],
        "max_ttl_seconds": env["max_ttl_seconds"],
    }
    print(json.dumps(result))
    return 0


def _cmd_authorize(args: argparse.Namespace) -> int:
    policy = load_policy(Path(args.policy))
    ok, reason = authorize(policy, args.environment_id, args.repo, args.ref, args.actor)
    print(json.dumps({"ok": ok, "reason": reason}))
    return 0 if ok else 1


def _cmd_check_endpoint(args: argparse.Namespace) -> int:
    policy = load_policy(Path(args.policy))
    ok, reason = check_endpoint(
        policy, args.environment_id, args.queue_url, args.tls_hostname, args.tls_sha256
    )
    print(json.dumps({"ok": ok, "reason": reason}))
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", default=str(DEFAULT_POLICY_PATH))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate-schema").set_defaults(func=_cmd_validate_schema)

    p_resolve = sub.add_parser("resolve")
    p_resolve.add_argument("--environment-id", required=True)
    p_resolve.set_defaults(func=_cmd_resolve)

    p_auth = sub.add_parser("authorize")
    p_auth.add_argument("--environment-id", required=True)
    p_auth.add_argument("--repo", required=True)
    p_auth.add_argument("--ref", required=True)
    p_auth.add_argument("--actor", required=True)
    p_auth.set_defaults(func=_cmd_authorize)

    p_check = sub.add_parser("check-endpoint")
    p_check.add_argument("--environment-id", required=True)
    p_check.add_argument("--queue-url", required=True)
    p_check.add_argument("--tls-hostname", required=True)
    p_check.add_argument("--tls-sha256", required=True)
    p_check.set_defaults(func=_cmd_check_endpoint)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
