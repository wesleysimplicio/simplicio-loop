"""Identity, context-isolation and receipt bindings for distributed workers.

The queue identifies *who* owns a lease; this module defines the smaller
contract that may cross a device boundary.  A context pack contains only the
assigned work item and allow-listed source references.  Prompts, transcripts,
environment variables and another worker's private state are never copied.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

SCHEMA = "simplicio.agent-context/v1"
RECEIPT_SCHEMA = "simplicio.agent-receipt/v1"
IDENTITY_FIELDS = ("agent_id", "runtime", "device_id", "session_id")
CAPABILITIES = frozenset(("claim", "heartbeat", "fencing", "receipts", "events", "evidence", "completion"))


class AgentContractError(ValueError):
    """Raised when an identity, capability set, or context pack is unsafe."""


def validate_identity(identity: Mapping[str, Any], *, capabilities: Sequence[str] | None = None) -> dict[str, Any]:
    if not isinstance(identity, Mapping):
        raise AgentContractError("agent identity must be an object")
    result = {field: str(identity.get(field) or "").strip() for field in IDENTITY_FIELDS}
    missing = [field for field, value in result.items() if not value]
    if missing:
        raise AgentContractError("agent identity missing: " + ", ".join(missing))
    raw_caps = list(capabilities if capabilities is not None else (identity.get("capabilities") or []))
    if any(not isinstance(cap, str) or not cap.strip() for cap in raw_caps):
        raise AgentContractError("capabilities must contain non-empty strings")
    normalized = [cap.strip() for cap in raw_caps]
    if len(set(normalized)) != len(normalized):
        raise AgentContractError("duplicate capabilities are not allowed")
    unknown = sorted(set(normalized).difference(CAPABILITIES))
    if unknown:
        raise AgentContractError("unsupported capabilities: " + ", ".join(unknown))
    result["capabilities"] = normalized
    result["protocol"] = str(identity.get("protocol") or "simplicio-distributed/v1")
    return result


def build_context_pack(*, task_id: str, goal: str, identity: Mapping[str, Any],
                       acs: Sequence[str] = (), source_refs: Sequence[str] = (),
                       depends_on: Sequence[str] = (), allowed_paths: Sequence[str] = ()) -> dict[str, Any]:
    """Build a deterministic, minimum-necessary context for one worker."""
    normalized = validate_identity(identity)
    allow = {str(path).strip() for path in allowed_paths if str(path).strip()}
    refs = [str(path).strip() for path in source_refs if str(path).strip() and str(path).strip() in allow]
    return {
        "schema": SCHEMA,
        "task_id": str(task_id).strip(),
        "goal": str(goal).strip(),
        "acs": [str(ac).strip() for ac in acs if str(ac).strip()],
        "source_refs": refs,
        "depends_on": [str(dep).strip() for dep in depends_on if str(dep).strip()],
        "assigned_to": {field: normalized[field] for field in IDENTITY_FIELDS},
        "capabilities": list(normalized["capabilities"]),
    }


def bind_receipt(receipt: Mapping[str, Any], identity: Mapping[str, Any], *, context_pack: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return a receipt carrying immutable agent identity and isolated context."""
    normalized = validate_identity(identity)
    result = deepcopy(dict(receipt))
    existing = result.get("agent")
    if existing is not None and validate_identity(existing) != normalized:
        raise AgentContractError("receipt agent identity mismatch")
    result["schema"] = result.get("schema") or RECEIPT_SCHEMA
    result["agent"] = normalized
    if context_pack is not None:
        if context_pack.get("assigned_to") != {field: normalized[field] for field in IDENTITY_FIELDS}:
            raise AgentContractError("context pack is assigned to another agent")
        result["context"] = deepcopy(dict(context_pack))
    return result


__all__ = ["AgentContractError", "CAPABILITIES", "IDENTITY_FIELDS", "RECEIPT_SCHEMA", "SCHEMA", "bind_receipt", "build_context_pack", "validate_identity"]
