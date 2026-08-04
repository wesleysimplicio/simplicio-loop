"""Canonical contracts for hierarchical Prism execution.

The module is deliberately stdlib-only and side-effect free.  It owns identity,
lineage, capacity, and authority validation; scheduling and mutation remain in
their respective layers.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import struct
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from .hbp_ledger import canonical_bytes, canonical_sha256

PRISM_SCHEMA = "simplicio.prism-execution/v1"
SLOT_SCHEMA = "simplicio.slot-supervisor/v1"
OWNERSHIP_SCHEMA = "simplicio.task-ownership/v1"
ADMISSION_SCHEMA = "simplicio.prism-admission/v1"
HBP_MAGIC = b"SPH1"
HBP_MAX_FRAME_BYTES = 8 * 1024 * 1024
MIN_TASKS_PER_SLOT = 10
# Compatibility markers only.  ``None`` makes the removed ceilings explicit
# and prevents callers from treating the old names as active limits.
MAX_TASKS_PER_SLOT = None
MAX_ACTIVE_SLOTS = None
MAX_PRISM_DEPTH = 4
PRISM_STATES = frozenset(
    {"declared", "running", "reducing", "completed", "partial", "blocked", "cancelled"}
)
SLOT_STATES = frozenset(
    {
        "declared",
        "ready",
        "running",
        "reducing",
        "completed",
        "partial",
        "blocked",
        "cancelled",
    }
)
TASK_STATES = frozenset(
    {
        "queued",
        "ready",
        "running",
        "validating",
        "accepted",
        "failed",
        "blocked",
        "cancelled",
    }
)
TERMINAL_TASK_STATES = frozenset({"accepted", "failed", "blocked", "cancelled"})


class PrismContractError(ValueError):
    """A Prism contract would widen authority or violate hierarchy invariants."""

    reason_code = "PRISM_CONTRACT_INVALID"


def _text(value: Any, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise PrismContractError(f"{name} is required")
    return result


def _tuple(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(sorted({_text(value, "sequence value") for value in values}))


def _sha(value: str, name: str) -> str:
    result = _text(value, name).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise PrismContractError(f"{name} must be a lowercase SHA-256")
    return result


def _mapping(value: Mapping[str, int] | None, name: str) -> tuple[tuple[str, int], ...]:
    result: list[tuple[str, int]] = []
    for key, raw in (value or {}).items():
        item = int(raw)
        if isinstance(raw, bool) or item < 0:
            raise PrismContractError(f"{name}.{key} must be a non-negative integer")
        result.append((_text(key, f"{name} key"), item))
    return tuple(sorted(result))


def content_id(prefix: str, value: Mapping[str, Any]) -> str:
    """Return a stable content-addressed identity.

    Collection fields are normalized by the dataclasses before this function is
    called, so input permutation cannot alter the identity.
    """
    return f"{_text(prefix, 'prefix')}:{canonical_sha256(value)}"


def _contract_dict(value: Any, *, include_identity: bool = True) -> dict[str, Any]:
    if not dataclasses.is_dataclass(value):
        raise TypeError("contract must be a dataclass")
    payload = dataclasses.asdict(value)
    if not include_identity:
        for key in ("prism_id", "slot_id", "ownership_id"):
            payload.pop(key, None)
    return payload


@dataclass(frozen=True)
class PrismExecution:
    goal_id: str
    owner_agent: str
    policy_hash: str
    config_hash: str
    source_generation: str
    reducer_ref: str
    parent_prism_id: str | None = None
    budget: tuple[tuple[str, int], ...] = ()
    child_slot_ids: tuple[str, ...] = ()
    state: str = "declared"
    max_depth: int = MAX_PRISM_DEPTH
    prism_id: str = ""
    schema: str = PRISM_SCHEMA

    def __post_init__(self) -> None:
        for name in ("goal_id", "owner_agent", "source_generation", "reducer_ref"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "policy_hash", _sha(self.policy_hash, "policy_hash"))
        object.__setattr__(self, "config_hash", _sha(self.config_hash, "config_hash"))
        object.__setattr__(
            self, "parent_prism_id", str(self.parent_prism_id or "").strip() or None
        )
        object.__setattr__(self, "child_slot_ids", _tuple(self.child_slot_ids))
        object.__setattr__(self, "budget", _mapping(dict(self.budget), "budget"))
        if self.schema != PRISM_SCHEMA:
            raise PrismContractError("unknown PrismExecution schema")
        if self.state not in PRISM_STATES:
            raise PrismContractError("unsupported prism state")
        if not 1 <= int(self.max_depth) <= MAX_PRISM_DEPTH:
            raise PrismContractError(f"max_depth must be in [1, {MAX_PRISM_DEPTH}]")
        identity_payload = _contract_dict(self, include_identity=False)
        identity_payload.pop("state", None)
        identity_payload.pop("child_slot_ids", None)
        identity = content_id("prism", identity_payload)
        if self.prism_id and self.prism_id != identity:
            raise PrismContractError("prism_id does not match canonical content")
        object.__setattr__(self, "prism_id", identity)

    def to_dict(self) -> dict[str, Any]:
        return _contract_dict(self)

    @property
    def digest(self) -> str:
        return canonical_sha256(self.to_dict())


@dataclass(frozen=True)
class SlotSupervisor:
    parent_prism_id: str
    supervisor_agent: str
    capacity: int = MIN_TASKS_PER_SLOT
    task_ids: tuple[str, ...] = ()
    child_slot_ids: tuple[str, ...] = ()
    parent_slot_id: str | None = None
    resource_budget: tuple[tuple[str, int], ...] = ()
    state: str = "declared"
    slot_id: str = ""
    schema: str = SLOT_SCHEMA

    def __post_init__(self) -> None:
        for name in ("parent_prism_id", "supervisor_agent"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(
            self, "parent_slot_id", str(self.parent_slot_id or "").strip() or None
        )
        object.__setattr__(self, "task_ids", _tuple(self.task_ids))
        object.__setattr__(self, "child_slot_ids", _tuple(self.child_slot_ids))
        object.__setattr__(
            self,
            "resource_budget",
            _mapping(dict(self.resource_budget), "resource_budget"),
        )
        if self.schema != SLOT_SCHEMA:
            raise PrismContractError("unknown SlotSupervisor schema")
        if self.state not in SLOT_STATES:
            raise PrismContractError("unsupported slot state")
        if isinstance(self.capacity, bool) or not isinstance(self.capacity, int):
            raise PrismContractError("slot capacity must be an integer")
        if int(self.capacity) < MIN_TASKS_PER_SLOT:
            raise PrismContractError(
                f"slot capacity must be at least {MIN_TASKS_PER_SLOT}"
            )
        if len(self.task_ids) > self.capacity:
            raise PrismContractError("slot exceeds declared task capacity")
        if self.slot_id in self.child_slot_ids:
            raise PrismContractError("slot cannot contain itself")
        identity_payload = _contract_dict(self, include_identity=False)
        for dynamic in ("task_ids", "child_slot_ids", "state"):
            identity_payload.pop(dynamic, None)
        identity = content_id("slot", identity_payload)
        if self.slot_id and self.slot_id != identity:
            raise PrismContractError("slot_id does not match canonical content")
        object.__setattr__(self, "slot_id", identity)

    def to_dict(self) -> dict[str, Any]:
        return _contract_dict(self)

    @property
    def digest(self) -> str:
        return canonical_sha256(self.to_dict())


@dataclass(frozen=True)
class TaskOwnership:
    task_id: str
    slot_id: str
    attempt: int
    owner_agent: str
    lease_id: str
    fence: int
    source_generation: str
    capabilities: tuple[str, ...]
    allowed_transitions: tuple[str, ...]
    causal_parent: str | None = None
    state: str = "queued"
    ownership_id: str = ""
    schema: str = OWNERSHIP_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "task_id",
            "slot_id",
            "owner_agent",
            "lease_id",
            "source_generation",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "capabilities", _tuple(self.capabilities))
        object.__setattr__(
            self, "allowed_transitions", _tuple(self.allowed_transitions)
        )
        object.__setattr__(
            self, "causal_parent", str(self.causal_parent or "").strip() or None
        )
        if self.schema != OWNERSHIP_SCHEMA:
            raise PrismContractError("unknown TaskOwnership schema")
        if isinstance(self.attempt, bool) or int(self.attempt) < 1:
            raise PrismContractError("attempt must be a positive integer")
        if isinstance(self.fence, bool) or int(self.fence) < 1:
            raise PrismContractError("fence must be a positive integer")
        if not self.capabilities:
            raise PrismContractError("at least one capability is required")
        if not self.allowed_transitions:
            raise PrismContractError("allowed_transitions cannot be empty")
        if not set(self.allowed_transitions) <= TASK_STATES:
            raise PrismContractError("unknown task transition")
        if self.state not in TASK_STATES:
            raise PrismContractError("unsupported task state")
        identity_payload = _contract_dict(self, include_identity=False)
        identity_payload.pop("state", None)
        identity = content_id("ownership", identity_payload)
        if self.ownership_id and self.ownership_id != identity:
            raise PrismContractError("ownership_id does not match canonical content")
        object.__setattr__(self, "ownership_id", identity)

    def to_dict(self) -> dict[str, Any]:
        return _contract_dict(self)

    @property
    def digest(self) -> str:
        return canonical_sha256(self.to_dict())

    def transition(self, state: str, *, fence: int, owner_agent: str) -> TaskOwnership:
        if (
            int(fence) != self.fence
            or _text(owner_agent, "owner_agent") != self.owner_agent
        ):
            raise PrismContractError("stale or non-owner transition")
        if state not in self.allowed_transitions:
            raise PrismContractError("transition is not authorized")
        return replace(self, state=state, ownership_id="")


@dataclass(frozen=True)
class AdmissionReceipt:
    slot_id: str
    task_id: str
    admitted: bool
    reason_code: str
    position: int | None
    slot_digest: str
    schema: str = ADMISSION_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        payload = dataclasses.asdict(self)
        payload["receipt_hash"] = canonical_sha256(payload)
        return payload


def admit_task(
    slot: SlotSupervisor, ownership: TaskOwnership
) -> tuple[SlotSupervisor, AdmissionReceipt]:
    """Admit one task or return an explicit queue decision for the 11th."""
    if ownership.slot_id != slot.slot_id:
        raise PrismContractError("ownership crosses slot")
    if ownership.task_id in slot.task_ids:
        raise PrismContractError("duplicate task admission")
    if len(slot.task_ids) >= slot.capacity:
        return slot, AdmissionReceipt(
            slot.slot_id,
            ownership.task_id,
            False,
            "SLOT_LOGICAL_CAPACITY",
            len(slot.task_ids) + 1,
            slot.digest,
        )
    updated = replace(
        slot,
        task_ids=slot.task_ids + (ownership.task_id,),
        slot_id="",
    )
    # A content-addressed slot changes identity when its immutable declaration
    # changes.  The receipt binds both the prior authority and the new digest.
    return updated, AdmissionReceipt(
        slot.slot_id,
        ownership.task_id,
        True,
        "ADMITTED",
        len(updated.task_ids),
        updated.digest,
    )


def validate_hierarchy(
    prisms: Sequence[PrismExecution],
    slots: Sequence[SlotSupervisor],
    ownerships: Sequence[TaskOwnership],
    *,
    max_depth: int = MAX_PRISM_DEPTH,
) -> dict[str, Any]:
    """Fail closed on cycles, orphan references, duplicate ownership, and width."""
    if not 1 <= int(max_depth) <= MAX_PRISM_DEPTH:
        raise PrismContractError("invalid hierarchy max_depth")
    prism_by_id = {item.prism_id: item for item in prisms}
    slot_by_id = {item.slot_id: item for item in slots}
    if len(prism_by_id) != len(prisms) or len(slot_by_id) != len(slots):
        raise PrismContractError("duplicate prism or slot identity")
    for prism in prisms:
        if prism.parent_prism_id and prism.parent_prism_id not in prism_by_id:
            raise PrismContractError("unknown parent prism")
        if any(slot_id not in slot_by_id for slot_id in prism.child_slot_ids):
            raise PrismContractError("prism references unknown child slot")
    for slot in slots:
        if slot.parent_prism_id not in prism_by_id:
            raise PrismContractError("slot references unknown prism")
        if slot.parent_slot_id and slot.parent_slot_id not in slot_by_id:
            raise PrismContractError("slot references unknown parent slot")
        if any(child not in slot_by_id for child in slot.child_slot_ids):
            raise PrismContractError("slot references unknown child slot")

    def check_parent_chain(
        node_id: str, parents: Mapping[str, str | None], kind: str
    ) -> int:
        seen: set[str] = set()
        depth = 0
        cursor: str | None = node_id
        while cursor:
            if cursor in seen:
                raise PrismContractError(f"{kind} hierarchy contains a cycle")
            seen.add(cursor)
            depth += 1
            if depth > max_depth:
                raise PrismContractError(f"{kind} hierarchy exceeds max_depth")
            cursor = parents.get(cursor)
        return depth

    prism_parents = {item.prism_id: item.parent_prism_id for item in prisms}
    slot_parents = {item.slot_id: item.parent_slot_id for item in slots}
    depths = [
        check_parent_chain(item.prism_id, prism_parents, "prism") for item in prisms
    ]
    depths.extend(
        check_parent_chain(item.slot_id, slot_parents, "slot") for item in slots
    )

    owner_by_task: dict[str, TaskOwnership] = {}
    declared_tasks = {task_id for slot in slots for task_id in slot.task_ids}
    for ownership in ownerships:
        if ownership.task_id in owner_by_task:
            raise PrismContractError("task has multiple accountable owners")
        if ownership.slot_id not in slot_by_id:
            raise PrismContractError("ownership references unknown slot")
        if ownership.task_id not in slot_by_id[ownership.slot_id].task_ids:
            raise PrismContractError("ownership task is not admitted by its slot")
        owner_by_task[ownership.task_id] = ownership
    if declared_tasks != set(owner_by_task):
        raise PrismContractError("every admitted task requires exactly one owner")

    payload = {
        "schema": "simplicio.prism-hierarchy-validation/v1",
        "prisms": sorted(prism_by_id),
        "slots": sorted(slot_by_id),
        "tasks": sorted(owner_by_task),
        "max_observed_depth": max(depths, default=0),
        "valid": True,
    }
    payload["digest"] = canonical_sha256(payload)
    return payload


def encode_hbp_frame(value: Mapping[str, Any]) -> bytes:
    """Encode one checksummed internal frame.

    JSON remains an external/debug representation.  Internal persistence uses a
    length-delimited frame with an explicit magic and trailing SHA-256.
    """
    payload = canonical_bytes(value)
    if len(payload) > HBP_MAX_FRAME_BYTES:
        raise PrismContractError("HBP frame exceeds size limit")
    return (
        HBP_MAGIC
        + struct.pack(">I", len(payload))
        + payload
        + hashlib.sha256(payload).digest()
    )


def decode_hbp_frame(frame: bytes) -> dict[str, Any]:
    if len(frame) < 40 or frame[:4] != HBP_MAGIC:
        raise PrismContractError("invalid HBP magic or truncated frame")
    size = struct.unpack(">I", frame[4:8])[0]
    if size > HBP_MAX_FRAME_BYTES or len(frame) != 8 + size + 32:
        raise PrismContractError("invalid HBP frame length")
    payload = frame[8 : 8 + size]
    if hashlib.sha256(payload).digest() != frame[-32:]:
        raise PrismContractError("HBP frame checksum mismatch")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrismContractError("invalid HBP payload") from exc
    if not isinstance(value, dict):
        raise PrismContractError("HBP payload must be an object")
    if canonical_bytes(value) != payload:
        raise PrismContractError("HBP payload is not canonical")
    return value


def read_legacy_task(value: Mapping[str, Any]) -> dict[str, Any]:
    """Expose legacy input for migration without granting Prism authority."""
    return {
        "schema": "simplicio.prism-legacy-read/v1",
        "legacy_schema": str(value.get("schema") or ""),
        "task_id": str(value.get("task_id") or ""),
        "authoritative": False,
        "reason_code": "LEGACY_NOT_AUTHORITATIVE",
    }


__all__ = [
    "ADMISSION_SCHEMA",
    "MAX_ACTIVE_SLOTS",
    "MAX_PRISM_DEPTH",
    "MAX_TASKS_PER_SLOT",
    "MIN_TASKS_PER_SLOT",
    "OWNERSHIP_SCHEMA",
    "PRISM_SCHEMA",
    "SLOT_SCHEMA",
    "AdmissionReceipt",
    "PrismContractError",
    "PrismExecution",
    "SlotSupervisor",
    "TaskOwnership",
    "admit_task",
    "content_id",
    "decode_hbp_frame",
    "encode_hbp_frame",
    "read_legacy_task",
    "validate_hierarchy",
]
