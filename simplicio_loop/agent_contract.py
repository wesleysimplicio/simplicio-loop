"""Identity, context-isolation and receipt bindings for distributed workers.

The queue identifies *who* owns a lease; this module defines the smaller
contract that may cross a device boundary.  A context pack contains only the
assigned work item and allow-listed source references.  Prompts, transcripts,
environment variables and another worker's private state are never copied.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Mapping, Sequence

SCHEMA = "simplicio.agent-context/v1"
RECEIPT_SCHEMA = "simplicio.agent-receipt/v1"
IDENTITY_FIELDS = ("agent_id", "runtime", "device_id", "session_id")
CONTEXT_FIELDS = (
    "schema", "task_id", "goal", "acs", "source_refs", "depends_on",
    "assigned_to", "capabilities", "issue_ref", "issue_url",
    "role_id", "role_version", "stage_id", "stage_version", "run_id",
    "work_item_id", "attempt_id", "fence", "plan_revision",
    "coordinator_agent_id", "parent_instance_id", "idempotency_key",
)
STAGE_CONTEXT_FIELDS = (
    "role_id", "role_version", "stage_id", "stage_version", "run_id",
    "task_id", "work_item_id", "attempt_id", "fence", "plan_revision",
    "coordinator_agent_id", "parent_instance_id", "idempotency_key",
)
CAPABILITIES = frozenset(("claim", "heartbeat", "fencing", "receipts", "events", "evidence", "completion"))
_ISSUE_REF_RE = re.compile(r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)#(?P<number>[1-9][0-9]*)$")
_ISSUE_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/issues/(?P<number>[1-9][0-9]*)/?$"
)
_STAGE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


class AgentContractError(ValueError):
    """Raised when an identity, capability set, or context pack is unsafe."""


def _canonical_issue_fields(issue_ref: Any = "", issue_url: Any = "") -> tuple[str, str]:
    raw_ref = str(issue_ref or "").strip()
    raw_url = str(issue_url or "").strip()
    if not raw_ref and not raw_url:
        return "", ""
    ref_match = _ISSUE_REF_RE.match(raw_ref) if raw_ref else None
    url_match = _ISSUE_URL_RE.match(raw_url) if raw_url else None
    if raw_ref and ref_match is None:
        raise AgentContractError("issue_ref must be canonical owner/repo#123")
    if raw_url and url_match is None:
        raise AgentContractError("issue_url must be canonical https://github.com/owner/repo/issues/123")
    ref_parts = ref_match.groupdict() if ref_match else None
    url_parts = url_match.groupdict() if url_match else None
    if ref_parts and url_parts and ref_parts != url_parts:
        raise AgentContractError("issue_ref and issue_url identify different issues")
    parts = ref_parts or url_parts or {}
    owner = parts.get("owner", "")
    repo = parts.get("repo", "")
    number = parts.get("number", "")
    return f"{owner}/{repo}#{number}", f"https://github.com/{owner}/{repo}/issues/{number}"


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
    for field in STAGE_CONTEXT_FIELDS:
        if field not in identity or identity.get(field) is None:
            continue
        value = identity[field]
        if field == "plan_revision":
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise AgentContractError("plan_revision must be a non-negative integer")
            result[field] = value
        else:
            value = str(value).strip()
            if not value:
                raise AgentContractError(field + " must be non-empty when provided")
            result[field] = value
    return result


def validate_context_pack(context_pack: Mapping[str, Any], identity: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(context_pack, Mapping):
        raise AgentContractError("context pack must be an object")
    extra = sorted(set(context_pack.keys()).difference(CONTEXT_FIELDS))
    if extra:
        raise AgentContractError("context pack contains non-allow-listed fields: " + ", ".join(extra))
    normalized = validate_identity(identity)
    assigned_to = context_pack.get("assigned_to")
    if assigned_to != {field: normalized[field] for field in IDENTITY_FIELDS}:
        raise AgentContractError("context pack is assigned to another agent")
    result = {
        "schema": str(context_pack.get("schema") or SCHEMA),
        "task_id": str(context_pack.get("task_id") or "").strip(),
        "goal": str(context_pack.get("goal") or "").strip(),
        "acs": [str(ac).strip() for ac in context_pack.get("acs", ()) if str(ac).strip()],
        "source_refs": [str(path).strip() for path in context_pack.get("source_refs", ()) if str(path).strip()],
        "depends_on": [str(dep).strip() for dep in context_pack.get("depends_on", ()) if str(dep).strip()],
        "assigned_to": dict(assigned_to),
        "capabilities": list(context_pack.get("capabilities") or ()),
    }
    for field in STAGE_CONTEXT_FIELDS:
        if field in context_pack and context_pack[field] is not None:
            expected = normalized.get(field)
            actual = context_pack[field]
            if expected is not None and actual != expected:
                raise AgentContractError("context pack " + field + " does not match agent identity")
            result[field] = actual
    result["issue_ref"], result["issue_url"] = _canonical_issue_fields(
        context_pack.get("issue_ref"),
        context_pack.get("issue_url"),
    )
    if result["schema"] != SCHEMA:
        raise AgentContractError("unsupported context pack schema")
    if not result["task_id"]:
        raise AgentContractError("context pack task_id is required")
    if not result["goal"]:
        raise AgentContractError("context pack goal is required")
    if result["capabilities"] != list(normalized["capabilities"]):
        raise AgentContractError("context pack capabilities do not match assigned agent")
    return result


def build_context_pack(*, task_id: str, goal: str, identity: Mapping[str, Any],
                       acs: Sequence[str] = (), source_refs: Sequence[str] = (),
                       depends_on: Sequence[str] = (), allowed_paths: Sequence[str] = (),
                       issue_ref: str = "", issue_url: str = "",
                       role_id: str | None = None, role_version: str | None = None,
                       stage_id: str | None = None, stage_version: str | None = None,
                       run_id: str | None = None, work_item_id: str | None = None,
                       attempt_id: str | None = None, fence: str | None = None,
                       plan_revision: int | None = None,
                       coordinator_agent_id: str | None = None,
                       parent_instance_id: str | None = None,
                       idempotency_key: str | None = None) -> dict[str, Any]:
    """Build a deterministic, minimum-necessary context for one worker."""
    normalized = validate_identity(identity)
    allow = {str(path).strip() for path in allowed_paths if str(path).strip()}
    refs = [str(path).strip() for path in source_refs if str(path).strip() and str(path).strip() in allow]
    payload = {
        "schema": SCHEMA,
        "task_id": str(task_id).strip(),
        "goal": str(goal).strip(),
        "acs": [str(ac).strip() for ac in acs if str(ac).strip()],
        "source_refs": refs,
        "depends_on": [str(dep).strip() for dep in depends_on if str(dep).strip()],
        "assigned_to": {field: normalized[field] for field in IDENTITY_FIELDS},
        "capabilities": list(normalized["capabilities"]),
        "issue_ref": str(issue_ref).strip(),
        "issue_url": str(issue_url).strip(),
    }
    overrides = {
        "role_id": role_id, "role_version": role_version, "stage_id": stage_id,
        "stage_version": stage_version, "run_id": run_id, "task_id": task_id,
        "work_item_id": work_item_id, "attempt_id": attempt_id, "fence": fence,
        "plan_revision": plan_revision, "coordinator_agent_id": coordinator_agent_id,
        "parent_instance_id": parent_instance_id, "idempotency_key": idempotency_key,
    }
    for field, value in overrides.items():
        if value is not None:
            payload[field] = value
    for field in STAGE_CONTEXT_FIELDS:
        if field not in payload and field in normalized:
            payload[field] = normalized[field]
    return validate_context_pack(payload, normalized)


def bind_receipt(receipt: Mapping[str, Any], identity: Mapping[str, Any], *, context_pack: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return a receipt carrying immutable agent identity and isolated context."""
    normalized = validate_identity(identity)
    result = deepcopy(dict(receipt))
    existing = result.get("agent")
    if existing is not None and validate_identity(existing) != normalized:
        raise AgentContractError("receipt agent identity mismatch")
    for field in STAGE_CONTEXT_FIELDS:
        if field in result and field in normalized and result[field] != normalized[field]:
            raise AgentContractError("receipt " + field + " does not match agent identity")
    result["schema"] = result.get("schema") or RECEIPT_SCHEMA
    result["agent"] = normalized
    if result["schema"] == RECEIPT_SCHEMA:
        result["legacy_unbound"] = True
    for field in STAGE_CONTEXT_FIELDS:
        if field in normalized:
            result[field] = normalized[field]
    if context_pack is not None:
        context = deepcopy(validate_context_pack(context_pack, normalized))
        result["context"] = context
        for field in STAGE_CONTEXT_FIELDS:
            if field in context:
                result[field] = context[field]
        if context.get("issue_ref"):
            result["issue_ref"] = context["issue_ref"]
            result["issue_url"] = context["issue_url"]
    return result


# --- Stage-agent contract glue (EPIC #422) --------------------------------- #
STAGE_AGENT_SCHEMA = "simplicio.stage-agent-binding/v1"
LIFECYCLE = ("created", "ready", "running", "terminal")
TERMINAL_STATUS = frozenset(("completed", "failed", "blocked", "cancelled", "quarantined"))
RECEIPT_TYPES = frozenset(("pass", "fail", "blocked", "skip"))


def validate_stage_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a stage-agent identity carrying role_id/stage_id/lifecycle.

    Extends ``validate_identity`` with the stage-agent-specific fields required
    by the portable stage-agent contract (EPIC #422): ``role_id`` and
    ``stage_id`` must be present and lowercase-kebab, and ``lifecycle`` must be
    one of the known phases.
    """
    base = validate_identity(identity)
    role_id = str(identity.get("role_id") or "").strip()
    stage_id = str(identity.get("stage_id") or "").strip()
    lifecycle = str(identity.get("lifecycle") or "created").strip()
    if not role_id or not _STAGE_ID_RE.match(role_id):
        raise AgentContractError("stage identity requires a valid role_id")
    if not stage_id or not _STAGE_ID_RE.match(stage_id):
        raise AgentContractError("stage identity requires a valid stage_id")
    if lifecycle not in LIFECYCLE:
        raise AgentContractError("lifecycle must be one of: " + ", ".join(LIFECYCLE))
    base["role_id"] = role_id
    base["stage_id"] = stage_id
    base["lifecycle"] = lifecycle
    return base


__all__ = ["AgentContractError", "CAPABILITIES", "CONTEXT_FIELDS", "IDENTITY_FIELDS", "RECEIPT_SCHEMA",
           "SCHEMA", "STAGE_AGENT_SCHEMA", "LIFECYCLE", "TERMINAL_STATUS", "RECEIPT_TYPES",
           "bind_receipt", "build_context_pack", "validate_context_pack", "validate_identity",
           "validate_stage_identity"]
