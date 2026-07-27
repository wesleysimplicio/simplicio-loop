"""Deterministic cross-repository Work Gap Ledger (issue #785).

The ledger is a pure, stdlib-only reducer.  It never infers completion from a
file, PR or green test: callers must attach typed evidence and use three
independent seats for implementation, verification and completion.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "simplicio.work-gap-ledger/v1"

PROGRESS_STATES = (
    "UNMAPPED",
    "OWNED",
    "PLANNED",
    "IMPLEMENTED",
    "VERIFIED",
    "INTEGRATED",
    "DELIVERED",
)
EXCEPTION_STATES = frozenset(
    {"BLOCKED", "DEFERRED", "N_A_APPROVED", "REGRESSED", "REVOKED", "UNVERIFIED"}
)
ALL_STATES = frozenset(PROGRESS_STATES) | EXCEPTION_STATES
TERMINAL_STATES = frozenset({"DELIVERED", "N_A_APPROVED"})
_REQUIRED_EVIDENCE = {
    "IMPLEMENTED": "implementation",
    "VERIFIED": "verification",
    "INTEGRATED": "integration",
    "DELIVERED": "delivery",
}


class LedgerError(ValueError):
    """A fail-closed Work Gap Ledger validation error."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _event_hash(previous_hash: str, event: Mapping[str, Any]) -> str:
    payload = b"simplicio.work-gap-ledger.event/v1\0" + previous_hash.encode("ascii")
    payload += b"\0" + _canonical(event)
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class Evidence:
    kind: str
    handle: str
    digest: str
    actor_id: str

    def __post_init__(self) -> None:
        if not self.kind or not self.handle or not self.actor_id:
            raise LedgerError("evidence fields must be non-empty")
        if len(self.digest) != 64 or any(c not in "0123456789abcdef" for c in self.digest):
            raise LedgerError("evidence digest must be lowercase sha256")

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "handle": self.handle,
            "digest": self.digest,
            "actor_id": self.actor_id,
        }


@dataclass(frozen=True)
class WorkGap:
    requirement_id: str
    acceptance_criterion_id: str
    state: str = "UNMAPPED"
    owner_project: str | None = None
    owner_agent: str | None = None
    dependencies: tuple[str, ...] = ()
    expected_evidence: tuple[str, ...] = ()
    delivery_target: str | None = None
    executor_id: str | None = None
    verifier_id: str | None = None
    completion_auditor_id: str | None = None
    evidence: tuple[Evidence, ...] = ()
    revision: int = 0

    @property
    def key(self) -> str:
        return f"{self.requirement_id}:{self.acceptance_criterion_id}"

    def __post_init__(self) -> None:
        if not self.requirement_id or not self.acceptance_criterion_id:
            raise LedgerError("requirement and acceptance criterion IDs are required")
        if self.state not in ALL_STATES:
            raise LedgerError(f"unknown work-gap state: {self.state}")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise LedgerError("duplicate dependencies are not allowed")
        if self.key in self.dependencies:
            raise LedgerError("a work gap cannot depend on itself")

    def as_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "acceptance_criterion_id": self.acceptance_criterion_id,
            "state": self.state,
            "owner_project": self.owner_project,
            "owner_agent": self.owner_agent,
            "dependencies": list(self.dependencies),
            "expected_evidence": list(self.expected_evidence),
            "delivery_target": self.delivery_target,
            "executor_id": self.executor_id,
            "verifier_id": self.verifier_id,
            "completion_auditor_id": self.completion_auditor_id,
            "evidence": [item.as_dict() for item in self.evidence],
            "revision": self.revision,
        }


@dataclass(frozen=True)
class LedgerEvent:
    sequence: int
    gap_key: str
    from_state: str
    to_state: str
    actor_id: str
    seat: str
    evidence: tuple[Evidence, ...]
    previous_hash: str
    hash: str

    def payload(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "gap_key": self.gap_key,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "actor_id": self.actor_id,
            "seat": self.seat,
            "evidence": [item.as_dict() for item in self.evidence],
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload(), "previous_hash": self.previous_hash, "hash": self.hash}


@dataclass
class WorkGapLedger:
    gaps: dict[str, WorkGap] = field(default_factory=dict)
    events: list[LedgerEvent] = field(default_factory=list)

    def register(self, gap: WorkGap) -> None:
        if gap.key in self.gaps:
            raise LedgerError(f"work gap already registered: {gap.key}")
        self.gaps[gap.key] = gap

    def assign_owner(
        self, key: str, *, owner_project: str, owner_agent: str, actor_id: str
    ) -> LedgerEvent:
        if not owner_project or not owner_agent:
            raise LedgerError("owner project and agent are required")
        gap = self._get(key)
        if gap.state != "UNMAPPED":
            raise LedgerError("ownership can only be assigned from UNMAPPED")
        self.gaps[key] = replace(
            gap, owner_project=owner_project, owner_agent=owner_agent
        )
        return self.transition(key, "OWNED", actor_id=actor_id, seat="coverage")

    def transition(
        self,
        key: str,
        to_state: str,
        *,
        actor_id: str,
        seat: str,
        evidence: Sequence[Evidence] = (),
        executor_id: str | None = None,
        verifier_id: str | None = None,
        completion_auditor_id: str | None = None,
    ) -> LedgerEvent:
        gap = self._get(key)
        if to_state not in ALL_STATES:
            raise LedgerError(f"unknown work-gap state: {to_state}")
        self._validate_transition(gap, to_state)
        updated = replace(
            gap,
            executor_id=executor_id or gap.executor_id,
            verifier_id=verifier_id or gap.verifier_id,
            completion_auditor_id=completion_auditor_id or gap.completion_auditor_id,
            evidence=gap.evidence + tuple(evidence),
            revision=gap.revision + 1,
        )
        self._validate_authority(updated, to_state, actor_id, seat)
        self._validate_evidence(updated, to_state)
        self._validate_dependencies(updated, to_state)
        previous_hash = self.events[-1].hash if self.events else "0" * 64
        payload = {
            "sequence": len(self.events) + 1,
            "gap_key": key,
            "from_state": gap.state,
            "to_state": to_state,
            "actor_id": actor_id,
            "seat": seat,
            "evidence": [item.as_dict() for item in evidence],
        }
        event = LedgerEvent(
            **payload,
            evidence=tuple(evidence),
            previous_hash=previous_hash,
            hash=_event_hash(previous_hash, payload),
        )
        self.gaps[key] = replace(updated, state=to_state)
        self.events.append(event)
        return event

    def _validate_transition(self, gap: WorkGap, to_state: str) -> None:
        if gap.state in TERMINAL_STATES:
            raise LedgerError("terminal work gaps require an immutable addendum/new revision")
        if to_state in EXCEPTION_STATES:
            if to_state == "N_A_APPROVED" and gap.state == "UNMAPPED":
                raise LedgerError("N_A_APPROVED requires an owner and explicit review")
            return
        if gap.state in EXCEPTION_STATES:
            allowed = "OWNED" if gap.owner_project and gap.owner_agent else "UNMAPPED"
            if to_state != allowed:
                raise LedgerError(f"exception recovery must re-enter at {allowed}")
            return
        expected_index = PROGRESS_STATES.index(gap.state) + 1
        if expected_index >= len(PROGRESS_STATES) or PROGRESS_STATES[expected_index] != to_state:
            raise LedgerError(f"illegal transition {gap.state} -> {to_state}")

    @staticmethod
    def _validate_authority(gap: WorkGap, state: str, actor_id: str, seat: str) -> None:
        if not actor_id:
            raise LedgerError("actor_id is required")
        if state == "IMPLEMENTED":
            if seat != "executor" or gap.executor_id != actor_id:
                raise LedgerError("IMPLEMENTED requires the executor seat")
        elif state == "VERIFIED":
            if seat != "verifier" or gap.verifier_id != actor_id:
                raise LedgerError("VERIFIED requires the verifier seat")
            if gap.verifier_id == gap.executor_id:
                raise LedgerError("executor and verifier must be independent")
        elif state == "DELIVERED":
            if seat != "completion" or gap.completion_auditor_id != actor_id:
                raise LedgerError("DELIVERED requires the completion seat")
            identities = {gap.executor_id, gap.verifier_id, gap.completion_auditor_id}
            if None in identities or len(identities) != 3:
                raise LedgerError("executor, verifier and completion auditor must be independent")

    @staticmethod
    def _validate_evidence(gap: WorkGap, state: str) -> None:
        required = _REQUIRED_EVIDENCE.get(state)
        if required and not any(item.kind == required for item in gap.evidence):
            raise LedgerError(f"{state} requires {required} evidence")
        if state in {"OWNED", "PLANNED", "IMPLEMENTED", "VERIFIED", "INTEGRATED", "DELIVERED"}:
            if not gap.owner_project or not gap.owner_agent:
                raise LedgerError(f"{state} requires an owner")
        if state in {"PLANNED", "IMPLEMENTED", "VERIFIED", "INTEGRATED", "DELIVERED"}:
            if not gap.expected_evidence or not gap.delivery_target:
                raise LedgerError(f"{state} requires expected evidence and delivery target")

    def _validate_dependencies(self, gap: WorkGap, state: str) -> None:
        if state not in {"INTEGRATED", "DELIVERED"}:
            return
        for dependency in gap.dependencies:
            dependency_gap = self._get(dependency)
            if dependency_gap.state not in TERMINAL_STATES:
                raise LedgerError(
                    f"dependency {dependency} is {dependency_gap.state}, not terminal"
                )

    def _get(self, key: str) -> WorkGap:
        try:
            return self.gaps[key]
        except KeyError as exc:
            raise LedgerError(f"unknown work gap: {key}") from exc

    def verify_chain(self) -> None:
        previous = "0" * 64
        for expected_sequence, event in enumerate(self.events, 1):
            if event.sequence != expected_sequence or event.previous_hash != previous:
                raise LedgerError("event chain sequence/link mismatch")
            if _event_hash(previous, event.payload()) != event.hash:
                raise LedgerError("event chain hash mismatch")
            previous = event.hash

    def snapshot(self) -> dict[str, Any]:
        self.verify_chain()
        return {
            "schema": SCHEMA,
            "gaps": [self.gaps[key].as_dict() for key in sorted(self.gaps)],
            "events": [event.as_dict() for event in self.events],
        }

    def digest(self) -> str:
        return hashlib.sha256(_canonical(self.snapshot())).hexdigest()

    def unresolved(self) -> tuple[WorkGap, ...]:
        return tuple(
            self.gaps[key]
            for key in sorted(self.gaps)
            if self.gaps[key].state not in TERMINAL_STATES
        )


def sha256_evidence(kind: str, handle: str, content: bytes, actor_id: str) -> Evidence:
    return Evidence(kind, handle, hashlib.sha256(content).hexdigest(), actor_id)
