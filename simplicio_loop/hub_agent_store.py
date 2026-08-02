"""Contract-only Hub stage-agent records.

The former local persistence authority was retired. Production callers must
use the MapperStore operations boundary; this module retains pure validation
and receipt helpers for import/compatibility tooling and fails closed if an
old caller tries to instantiate the removed local store.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping, Sequence

JOB_SCHEMA = "contracts/hub-agent/v1/job"
HANDLE_SCHEMA = "contracts/hub-agent/v1/handle"
RECEIPT_SCHEMA = "contracts/hub-agent/v1/execution-receipt"
STORE_SCHEMA_VERSION = 1
TERMINAL_STATES = frozenset(("succeeded", "failed", "cancelled", "timed_out"))
STATES = frozenset(("prepared", "queued", "leased", "running", "recovery_unknown")) | TERMINAL_STATES
_TRANSITIONS = {
    "prepared": frozenset(("queued",)),
    "queued": frozenset(("leased",)),
    "leased": frozenset(("running", "queued", "recovery_unknown")),
    "running": TERMINAL_STATES | frozenset(("recovery_unknown",)),
    "recovery_unknown": frozenset(("queued",) + tuple(TERMINAL_STATES)),
}
_REQUIRED_IDS = ("graph_id", "run_id", "task_id", "stage_id", "role", "attempt_id")


class HubAgentStoreError(RuntimeError):
    reason_code = "hub_agent_store_error"


class ValidationError(HubAgentStoreError):
    reason_code = "invalid_record"


class IdempotencyConflict(HubAgentStoreError):
    reason_code = "idempotency_conflict"


class TransitionConflict(HubAgentStoreError):
    reason_code = "transition_conflict"


class LegacyStoreRemoved(HubAgentStoreError):
    reason_code = "legacy_store_removed"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 4096:
        raise ValidationError("%s must be non-empty bounded text" % name)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValidationError("%s contains control characters" % name)
    return value


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValidationError("%s must be a lowercase sha256" % name)
    return value


def _validate_process_spec(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError("process_spec must be an object")
    spec = dict(value)
    argv = spec.get("argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x for x in argv):
        raise ValidationError("process_spec.argv must be non-empty strings")
    if spec.get("shell", False) is not False:
        raise ValidationError("shell process specs are forbidden")
    return spec


def build_job(*, idempotency_key: str, source_fence: str, plan_revision: str,
              input_hash: str, context_hash: str, process_spec: Mapping[str, Any],
              deadline: str, priority: int, resources: Mapping[str, Any], **identifiers: str) -> Dict[str, Any]:
    job = {"schema": JOB_SCHEMA, "idempotency_key": _text(idempotency_key, "idempotency_key"),
           "source_fence": _text(source_fence, "source_fence"),
           "plan_revision": _text(plan_revision, "plan_revision"),
           "input_hash": _hash(input_hash, "input_hash"), "context_hash": _hash(context_hash, "context_hash"),
           "process_spec": _validate_process_spec(process_spec), "deadline": _text(deadline, "deadline"),
           "priority": priority, "resources": dict(resources)}
    for name in _REQUIRED_IDS:
        job[name] = _text(identifiers.get(name), name)
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise ValidationError("priority must be an integer")
    if not isinstance(resources, Mapping):
        raise ValidationError("resources must be an object")
    job["content_hash"] = _digest(job)
    return job


def validate_job(value: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError("job must be an object")
    raw = dict(value)
    expected = raw.pop("content_hash", None)
    if raw.get("schema") != JOB_SCHEMA or expected != _digest(raw):
        raise ValidationError("job schema or content hash is invalid")
    rebuilt = build_job(**{k: v for k, v in raw.items() if k != "schema"})
    if rebuilt != dict(value):
        raise ValidationError("job content is invalid")
    return rebuilt


def validate_receipt(value: Mapping[str, Any], *, job_id: str, generation: int, fence: str,
                     terminal_state: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError("receipt must be an object")
    receipt = dict(value)
    expected = receipt.pop("receipt_hash", None)
    if (receipt.get("schema") != RECEIPT_SCHEMA or receipt.get("job_id") != job_id
            or receipt.get("generation") != generation or receipt.get("fence") != fence
            or receipt.get("terminal_state") != terminal_state or expected != _digest(receipt)):
        raise ValidationError("receipt identity or hash is invalid")
    receipt["receipt_hash"] = expected
    return receipt


class HubAgentStore:
    """Retired compatibility name; no local persistence is allowed."""

    def __init__(self, path: Any) -> None:
        raise LegacyStoreRemoved(
            "HubAgentStore local persistence was removed; use MapperStore operations"
        )


def build_receipt(*, job_id: str, generation: int, fence: str, terminal_state: str,
                  outcome: Mapping[str, Any], evidence_hashes: Sequence[str]) -> Dict[str, Any]:
    if terminal_state not in TERMINAL_STATES:
        raise ValidationError("receipt state must be terminal")
    receipt = {"schema": RECEIPT_SCHEMA, "job_id": _text(job_id, "job_id"), "generation": generation,
               "fence": _text(fence, "fence"), "terminal_state": terminal_state,
               "outcome": dict(outcome), "evidence_hashes": [_hash(value, "evidence_hash") for value in evidence_hashes]}
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ValidationError("generation must be positive")
    receipt["receipt_hash"] = _digest(receipt)
    return receipt
