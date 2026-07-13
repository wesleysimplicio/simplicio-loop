#!/usr/bin/env python3
"""Fail-closed external probe for issue #183 AC6.

This probe validates only the remote queue security envelope and lane contract:

- queue URL is explicitly HTTPS,
- bearer token is present,
- TLS hostname is explicit and must match the queue URL hostname,
- TLS certificate SHA-256 fingerprint matches the expected value,
- the payload exposes exactly two runtime lanes: ``codex`` and ``claude``,
- when a real endpoint is provisioned, a read-only ``/v1/queue/events`` probe
  succeeds with the configured token.

Missing configuration or a skipped network probe never produces a success
result. The output is intentionally bounded: it does not claim multi-machine or
production completion proof.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import ssl
import urllib.parse
import urllib.request
from typing import Any, Mapping

SCHEMA = "simplicio.distributed-183-ac6-probe/v1"
ISSUE = 183
SLICE = "AC6"
ENV_URL = "SIMPLICIO_REMOTE_QUEUE_URL"
ENV_TOKEN = "SIMPLICIO_REMOTE_QUEUE_TOKEN"
ENV_TLS_HOSTNAME = "SIMPLICIO_REMOTE_QUEUE_TLS_HOSTNAME"
ENV_TLS_FINGERPRINT = "SIMPLICIO_REMOTE_QUEUE_TLS_FINGERPRINT"
ENV_PAYLOAD = "SIMPLICIO_DISTRIBUTED_183_PAYLOAD"
REQUIRED_ENV = (ENV_URL, ENV_TOKEN, ENV_TLS_HOSTNAME, ENV_TLS_FINGERPRINT, ENV_PAYLOAD)


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_fingerprint(value: str) -> str:
    compact = "".join(ch for ch in _normalize_text(value).lower() if ch in "0123456789abcdef")
    if len(compact) != 64:
        raise ValueError("TLS fingerprint must be exactly 64 hex characters (SHA-256)")
    return compact


def _mask_token(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return digest[:12]


def _parse_payload(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("distributed payload must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("distributed payload must be a JSON object")
    lanes = payload.get("lanes")
    if not isinstance(lanes, list):
        raise ValueError("distributed payload must include a lanes list")
    if len(lanes) != 2:
        raise ValueError("distributed payload must expose exactly two lanes")
    runtimes: list[str] = []
    for index, lane in enumerate(lanes):
        if not isinstance(lane, dict):
            raise ValueError("lane %d must be a JSON object" % index)
        runtime = _normalize_text(lane.get("runtime")).lower()
        agent_id = _normalize_text(lane.get("agent_id"))
        lane_id = _normalize_text(lane.get("lane_id"))
        if runtime not in {"codex", "claude"}:
            raise ValueError("lane %d runtime must be codex or claude" % index)
        if not agent_id:
            raise ValueError("lane %d agent_id is required" % index)
        if not lane_id:
            raise ValueError("lane %d lane_id is required" % index)
        runtimes.append(runtime)
    if sorted(runtimes) != ["claude", "codex"]:
        raise ValueError("distributed payload must include one codex lane and one claude lane")
    return payload


def inspect_tls_endpoint(queue_url: str, expected_hostname: str, timeout: float = 5.0) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(queue_url)
    if parsed.scheme != "https":
        raise ValueError("queue URL must use https")
    if not parsed.hostname:
        raise ValueError("queue URL must include a hostname")
    if parsed.hostname != expected_hostname:
        raise ValueError("queue URL hostname must exactly match the expected TLS hostname")
    context = ssl.create_default_context()
    port = parsed.port or 443
    with socket.create_connection((parsed.hostname, port), timeout=timeout) as raw_socket:
        with context.wrap_socket(raw_socket, server_hostname=expected_hostname) as tls_socket:
            certificate = tls_socket.getpeercert(binary_form=True)
            if not certificate:
                raise ValueError("peer certificate is missing")
            cipher = tls_socket.cipher()
            return {
                "hostname": expected_hostname,
                "port": port,
                "fingerprint_sha256": hashlib.sha256(certificate).hexdigest(),
                "tls_version": tls_socket.version() or "",
                "cipher": cipher[0] if cipher else "",
            }


def probe_queue_events(queue_url: str, token: str, timeout: float = 5.0) -> dict[str, Any]:
    request = urllib.request.Request(
        queue_url.rstrip("/") + "/v1/queue/events",
        data=json.dumps({"after": 0, "limit": 1}, sort_keys=True).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("queue probe response must be a JSON object")
    events = payload.get("events")
    if not isinstance(events, list):
        raise ValueError("queue probe response must contain an events list")
    return {
        "http_status": getattr(response, "status", 200),
        "event_count": len(events),
    }


def build_receipt(
    *,
    env: Mapping[str, str] | None = None,
    execute_real: bool | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    source_env = dict(os.environ if env is None else env)
    queue_url = _normalize_text(source_env.get(ENV_URL))
    token = _normalize_text(source_env.get(ENV_TOKEN))
    tls_hostname = _normalize_text(source_env.get(ENV_TLS_HOSTNAME))
    payload_raw = _normalize_text(source_env.get(ENV_PAYLOAD))
    expected_fingerprint_raw = _normalize_text(source_env.get(ENV_TLS_FINGERPRINT))

    checks: dict[str, bool] = {
        "queue_url_https": False,
        "token_present": False,
        "tls_hostname_explicit": False,
        "tls_fingerprint_explicit": False,
        "runtime_lanes_valid": False,
        "real_endpoint_probe": False,
    }
    errors: list[str] = []
    tls_details: dict[str, Any] = {}
    probe_details: dict[str, Any] = {}
    payload_summary: dict[str, Any] = {}

    missing_env = [name for name in REQUIRED_ENV if not _normalize_text(source_env.get(name))]
    parsed_url = urllib.parse.urlsplit(queue_url) if queue_url else urllib.parse.SplitResult("", "", "", "", "")
    checks["queue_url_https"] = bool(queue_url and parsed_url.scheme == "https" and parsed_url.hostname)
    checks["token_present"] = bool(token)
    checks["tls_hostname_explicit"] = bool(tls_hostname)
    checks["tls_fingerprint_explicit"] = bool(expected_fingerprint_raw)

    expected_fingerprint = ""
    if expected_fingerprint_raw:
        try:
            expected_fingerprint = _normalize_fingerprint(expected_fingerprint_raw)
        except ValueError as exc:
            errors.append(str(exc))

    if payload_raw:
        try:
            payload = _parse_payload(payload_raw)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            lanes = payload["lanes"]
            checks["runtime_lanes_valid"] = True
            payload_summary = {
                "lane_count": len(lanes),
                "runtimes": [str(lane["runtime"]).lower() for lane in lanes],
                "lane_ids": [str(lane["lane_id"]) for lane in lanes],
            }

    if execute_real is None:
        execute_real = bool(queue_url)

    if execute_real and not checks["queue_url_https"]:
        errors.append("queue URL must be provisioned as HTTPS before the real probe can run")

    if execute_real and not missing_env and not errors:
        try:
            tls_details = inspect_tls_endpoint(queue_url, tls_hostname, timeout=timeout)
        except Exception as exc:  # pragma: no cover - exercised via monkeypatch/unit seams
            errors.append("TLS probe failed: %s" % exc)
        else:
            if tls_details["fingerprint_sha256"] != expected_fingerprint:
                errors.append("TLS fingerprint mismatch")
            else:
                try:
                    probe_details = probe_queue_events(queue_url, token, timeout=timeout)
                except Exception as exc:  # pragma: no cover - exercised via monkeypatch/unit seams
                    errors.append("queue probe failed: %s" % exc)
                else:
                    checks["real_endpoint_probe"] = True
    elif not execute_real:
        errors.append("real endpoint probe was not executed")
    elif missing_env:
        errors.append("missing required env: %s" % ", ".join(missing_env))

    status = "VERIFIED" if all(checks.values()) and not errors else "UNVERIFIED"
    return {
        "schema": SCHEMA,
        "issue": ISSUE,
        "slice": SLICE,
        "status": status,
        "fail_closed": True,
        "epic_closure_ready": False,
        "probe_mode": "real" if execute_real else "config-only",
        "checks": checks,
        "missing_env": missing_env,
        "errors": errors,
        "config": {
            "queue_url": queue_url,
            "queue_hostname": parsed_url.hostname or "",
            "tls_hostname": tls_hostname,
            "expected_fingerprint_sha256": expected_fingerprint,
            "token_sha256_prefix": _mask_token(token) if token else "",
        },
        "payload_summary": payload_summary,
        "tls": tls_details,
        "probe": probe_details,
        "claim_boundary": (
            "UNVERIFIED| this probe validates only HTTPS queue configuration, TLS identity, "
            "token authentication, and the Codex/Claude lane contract; it does not prove "
            "external multi-machine completion or production convergence"
        ),
    }


def run(
    *,
    env: Mapping[str, str] | None = None,
    execute_real: bool | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    return build_receipt(env=env, execute_real=execute_real, timeout=timeout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument(
        "--config-only",
        action="store_true",
        help="validate config/payload only; stays UNVERIFIED because no real endpoint probe runs",
    )
    args = parser.parse_args(argv)
    receipt = run(execute_real=not args.config_only, timeout=args.timeout)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if receipt["status"] == "VERIFIED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
