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
from typing import Any, Dict, Tuple
from urllib.parse import urlsplit

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
        pins = env.get("tls_sha256_pins")
        if not isinstance(pins, list) or not pins or not all(isinstance(p, str) and p for p in pins):
            raise TrustPolicyError(f"environment '{env_id}' tls_sha256_pins must be a non-empty list of strings")
        for list_field in ("allowed_repos", "allowed_refs", "allowed_actors"):
            if not isinstance(env.get(list_field), list):
                raise TrustPolicyError(f"environment '{env_id}' {list_field} must be a list")


def resolve_environment(policy: Dict[str, Any], environment_id: str) -> Dict[str, Any]:
    environments = policy.get("environments", {})
    if environment_id not in environments:
        raise TrustPolicyError(
            f"unknown environment_id '{environment_id}': not present in trust policy"
        )
    return environments[environment_id]


def authorize(
    policy: Dict[str, Any], environment_id: str, repo: str, ref: str, actor: str
) -> Tuple[bool, str]:
    try:
        env = resolve_environment(policy, environment_id)
    except TrustPolicyError as exc:
        return False, str(exc)
    allowed_repos = env.get("allowed_repos") or []
    allowed_refs = env.get("allowed_refs") or []
    allowed_actors = env.get("allowed_actors") or []
    if repo not in allowed_repos:
        return False, f"repo '{repo}' is not authorized for environment '{environment_id}'"
    if ref not in allowed_refs:
        return False, f"ref '{ref}' is not authorized for environment '{environment_id}'"
    if allowed_actors and actor not in allowed_actors:
        return False, f"actor '{actor}' is not authorized for environment '{environment_id}'"
    return True, "authorized"


def _normalized_origin(scheme: str, hostname: str, port: int) -> str:
    return f"{scheme}://{hostname.lower().rstrip('.')}:{port}"


def check_endpoint(
    policy: Dict[str, Any],
    environment_id: str,
    queue_url: str,
    tls_hostname: str,
    tls_sha256: str,
) -> Tuple[bool, str]:
    """Fail closed unless the operationally-configured endpoint matches the policy exactly.

    The policy is always the anchor: this never derives trust from `queue_url` /
    `tls_hostname` / `tls_sha256` themselves, it only checks whether an out-of-band value
    (e.g. a GitHub Environment variable) still matches what the reviewed policy expects.
    """
    try:
        env = resolve_environment(policy, environment_id)
    except TrustPolicyError as exc:
        return False, str(exc)
    origin_cfg = env["origin"]
    expected_origin = _normalized_origin(origin_cfg["scheme"], origin_cfg["hostname"], int(origin_cfg["port"]))

    parsed = urlsplit(queue_url or "")
    if parsed.scheme != "https" or not parsed.hostname:
        return False, "queue_url must be an https URL with a hostname"
    if parsed.username or parsed.password:
        return False, "queue_url must not carry userinfo"
    actual_port = parsed.port or 443
    actual_origin = _normalized_origin(parsed.scheme, parsed.hostname, actual_port)
    if actual_origin != expected_origin:
        return False, f"queue_url origin '{actual_origin}' does not match policy origin '{expected_origin}'"

    if tls_hostname.lower().rstrip(".") != origin_cfg["hostname"].lower().rstrip("."):
        return False, "tls_hostname does not match policy hostname"

    normalized_fp = (tls_sha256 or "").replace(":", "").lower()
    pins = {p.replace(":", "").lower() for p in env["tls_sha256_pins"]}
    if normalized_fp not in pins:
        return False, "tls_sha256 does not match any pin in the policy"

    return True, "endpoint matches trust policy"


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
