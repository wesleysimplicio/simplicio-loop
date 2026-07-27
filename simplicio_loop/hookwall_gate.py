"""Fail-closed Hookwall boundary for mutable Loop dispatches (issue #783).

This module is deliberately transport-neutral.  Runtime and Dev CLI payloads are
untrusted mappings until the Loop validates the pre decision, mutation receipt,
and post decision as one lineage-bound chain.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, MutableSet

ENVELOPE_SCHEMA = "simplicio.dispatch-envelope/v1"
DECISION_SCHEMA = "simplicio.hookwall-decision/v1"
RECEIPT_SCHEMA = "simplicio.mutation-receipt/v1"
EVIDENCE_SCHEMA = "simplicio.hookwall-evidence/v1"

_ALLOWED_EFFECTS = frozenset({"read", "write", "process", "exclusive"})
_REQUIRED_ENVELOPE = (
    "envelope_id", "run_id", "plan_id", "source_hash", "policy_hash",
    "idempotency_key", "workspace", "fence", "effect_set",
)


class HookwallBlocked(RuntimeError):
    """A mutable dispatch failed closed before completion."""

    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code}: {detail}")


def _hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return str(value or "").strip()


def validate_envelope(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Return a normalized envelope or block malformed/unknown effects."""
    if envelope.get("schema") != ENVELOPE_SCHEMA:
        raise HookwallBlocked("invalid_envelope_schema", "DispatchEnvelopeV1 is required")
    missing = [key for key in _REQUIRED_ENVELOPE if not envelope.get(key)]
    if missing:
        raise HookwallBlocked("invalid_envelope", "missing: " + ", ".join(missing))
    effects = tuple(sorted({_text(item) for item in envelope["effect_set"]}))
    if not effects or "effect_unknown" in effects:
        raise HookwallBlocked("effect_unknown", "effect set must be resolved before dispatch")
    unknown = sorted(set(effects) - _ALLOWED_EFFECTS)
    if unknown:
        raise HookwallBlocked("effect_unknown", "unsupported effects: " + ", ".join(unknown))
    normalized = dict(envelope)
    normalized["effect_set"] = list(effects)
    normalized["envelope_hash"] = _hash({
        key: normalized[key] for key in sorted(normalized) if key != "envelope_hash"
    })
    supplied = envelope.get("envelope_hash")
    if supplied and supplied != normalized["envelope_hash"]:
        raise HookwallBlocked("envelope_hash_mismatch", "envelope was modified after sealing")
    return normalized


def validate_pre_decision(
    envelope: Mapping[str, Any],
    decision: Mapping[str, Any] | None,
    *,
    seen_idempotency_keys: MutableSet[str] | None = None,
) -> dict[str, Any]:
    """Authorize one dispatch only after an explicit Hookwall pre decision."""
    env = validate_envelope(envelope)
    if not decision:
        raise HookwallBlocked("hookwall_pre_missing", "mutable dispatch has no pre decision")
    if decision.get("schema") != DECISION_SCHEMA or decision.get("phase") != "pre":
        raise HookwallBlocked("hookwall_pre_invalid", "a HookwallDecisionV1 pre decision is required")
    if decision.get("verdict") != "proceed":
        raise HookwallBlocked(_text(decision.get("reason_code")) or "hookwall_pre_blocked",
                              "pre-hook did not authorize the effect")
    for key in ("envelope_id", "source_hash", "policy_hash", "fence"):
        if _text(decision.get(key)) != _text(env.get(key)):
            raise HookwallBlocked("hookwall_lineage_mismatch", f"pre decision {key} mismatch")
    if decision.get("envelope_hash") != env["envelope_hash"]:
        raise HookwallBlocked("hookwall_lineage_mismatch", "pre decision envelope hash mismatch")
    key = _text(env["idempotency_key"])
    if seen_idempotency_keys is not None and key in seen_idempotency_keys:
        raise HookwallBlocked("duplicate_effect", "idempotency key was already committed")
    return env


def verify_post_receipt(
    envelope: Mapping[str, Any],
    pre_decision: Mapping[str, Any],
    receipt: Mapping[str, Any] | None,
    post_decision: Mapping[str, Any] | None,
    *,
    seen_idempotency_keys: MutableSet[str] | None = None,
) -> dict[str, Any]:
    """Verify post-hook + mutation receipt and return compact completion evidence."""
    env = validate_pre_decision(envelope, pre_decision)
    if not receipt or receipt.get("schema") != RECEIPT_SCHEMA:
        raise HookwallBlocked("mutation_receipt_missing", "MutationReceiptV1 is required")
    if not post_decision or post_decision.get("schema") != DECISION_SCHEMA:
        raise HookwallBlocked("hookwall_post_missing", "HookwallDecisionV1 post decision is required")
    if post_decision.get("phase") != "post" or post_decision.get("verdict") != "proceed":
        raise HookwallBlocked(_text(post_decision.get("reason_code")) or "hookwall_post_blocked",
                              "post-hook did not verify the effect")
    for payload_name, payload in (("receipt", receipt), ("post decision", post_decision)):
        for key in ("envelope_id", "source_hash", "policy_hash", "idempotency_key", "fence"):
            if _text(payload.get(key)) != _text(env.get(key)):
                raise HookwallBlocked("hookwall_lineage_mismatch", f"{payload_name} {key} mismatch")
    receipt_payload = {k: receipt[k] for k in sorted(receipt) if k != "receipt_hash"}
    receipt_hash = _hash(receipt_payload)
    if receipt.get("receipt_hash") != receipt_hash:
        raise HookwallBlocked("mutation_receipt_hash_mismatch", "receipt content hash is invalid")
    if post_decision.get("receipt_hash") != receipt_hash:
        raise HookwallBlocked("hookwall_lineage_mismatch", "post decision is not bound to receipt")
    if receipt.get("status") not in {"committed", "verified"}:
        raise HookwallBlocked("effect_not_committed", "receipt does not prove a committed effect")
    key = _text(env["idempotency_key"])
    if seen_idempotency_keys is not None:
        if key in seen_idempotency_keys:
            raise HookwallBlocked("duplicate_effect", "idempotency key was already committed")
        seen_idempotency_keys.add(key)
    evidence = {
        "schema": EVIDENCE_SCHEMA,
        "envelope_id": env["envelope_id"],
        "envelope_hash": env["envelope_hash"],
        "receipt_hash": receipt_hash,
        "idempotency_key": key,
        "fence": env["fence"],
        "verdict": "verified",
    }
    evidence["evidence_hash"] = _hash(evidence)
    return evidence


def gate_completion(evidence: Mapping[str, Any] | None) -> tuple[bool, str]:
    """Keep completion authority in Loop; Runtime/Dev CLI evidence is necessary only."""
    if not evidence or evidence.get("schema") != EVIDENCE_SCHEMA:
        return False, "hookwall_evidence_missing"
    payload = {k: evidence[k] for k in sorted(evidence) if k != "evidence_hash"}
    if evidence.get("evidence_hash") != _hash(payload):
        return False, "hookwall_evidence_hash_mismatch"
    if evidence.get("verdict") != "verified":
        return False, "hookwall_effect_unverified"
    return True, "ok"


__all__ = [
    "ENVELOPE_SCHEMA", "DECISION_SCHEMA", "RECEIPT_SCHEMA", "EVIDENCE_SCHEMA",
    "HookwallBlocked", "validate_envelope", "validate_pre_decision",
    "verify_post_receipt", "gate_completion",
]
