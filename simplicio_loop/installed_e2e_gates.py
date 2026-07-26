"""Fail-closed watcher and negative-lane gates for installed E2E (#693)."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Optional

WATCHER_SCHEMA = "simplicio.independent-watcher-receipt/v1"
NEGATIVE_LANES = (
    "runtime_missing",
    "wrong_runtime_binary",
    "mapper_capability_missing",
    "dev_cli_capability_missing",
    "disconnect_after_effect",
    "corrupt_hbp_link",
    "stale_mapper_artifact",
    "watcher_mismatch",
    "duplicate_idempotency_key",
    "direct_mutation_bypass",
    "version_schema_mismatch",
    "cancellation_restart",
)


class InstalledGateError(RuntimeError):
    """The installed E2E evidence is malformed."""


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def verify_watcher_receipt(
    receipt: Optional[Mapping[str, Any]],
    *,
    challenge: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Fail closed unless an independent watcher proves this exact causal run."""
    if not isinstance(receipt, Mapping):
        return {"status": "BLOCKED", "reason": "watcher_receipt_missing"}
    checks = {
        "schema": receipt.get("schema") == WATCHER_SCHEMA,
        "independent": receipt.get("producer", {}).get("worker")
        == "independent_watcher.py",
        "challenge": bool(challenge) and receipt.get("challenge") == challenge,
        "correlation": receipt.get("correlation_id") == correlation_id,
        "match": receipt.get("status") == "MEASURED" and receipt.get("match") is True,
        "hbp": bool(receipt.get("hbp_receipt_hash")),
        "criteria": bool(receipt.get("criteria_results"))
        and all(item.get("status") == "PASS" for item in receipt["criteria_results"]),
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    return {
        "status": "READY" if not failed else "BLOCKED",
        "reason": "" if not failed else "watcher_receipt_invalid:" + ",".join(failed),
        "checks": checks,
        "receipt_hash": _digest(receipt),
    }


def verify_negative_lane(lane: str, evidence: Mapping[str, Any]) -> dict[str, str]:
    """Re-check one injected failure; labels alone never count as evidence."""
    if lane not in NEGATIVE_LANES:
        raise InstalledGateError("unknown_negative_lane")
    blocked = evidence.get("status") == "BLOCKED"
    effect_free = evidence.get("effects_authorized") is False
    reason = str(evidence.get("reason") or "")
    specific = {
        "runtime_missing": "binary_missing",
        "wrong_runtime_binary": "product_identity",
        "mapper_capability_missing": "mapper_capability_missing",
        "dev_cli_capability_missing": "dev_cli_capability_missing",
        "disconnect_after_effect": "outcome_unknown",
        "corrupt_hbp_link": "hbp_hash_mismatch",
        "stale_mapper_artifact": "mapper_artifact_stale",
        "watcher_mismatch": "watcher_challenge_mismatch",
        "duplicate_idempotency_key": "duplicate_idempotency_key",
        "direct_mutation_bypass": "direct_mutation_blocked",
        "version_schema_mismatch": "compatibility_mismatch",
        "cancellation_restart": "cancelled_not_replayed",
    }[lane]
    verified = blocked and effect_free and reason == specific
    return {
        "status": "PASS" if verified else "FAIL",
        "reason": specific if verified else "negative_lane_unproven",
    }


__all__ = [
    "InstalledGateError",
    "NEGATIVE_LANES",
    "WATCHER_SCHEMA",
    "verify_negative_lane",
    "verify_watcher_receipt",
]
