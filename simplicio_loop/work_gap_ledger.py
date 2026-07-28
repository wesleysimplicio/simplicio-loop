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
    expected_revision: str | None = None
    installed_artifact: Mapping[str, Any] | None = None
    source_requery: Mapping[str, Any] | None = None

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
            "expected_revision": self.expected_revision,
            "installed_artifact": (
                dict(self.installed_artifact) if self.installed_artifact is not None else None
            ),
            "source_requery": (
                dict(self.source_requery) if self.source_requery is not None else None
            ),
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
    facts: Mapping[str, Any]
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
            "facts": dict(self.facts),
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
        expected_revision: str | None = None,
        installed_artifact: Mapping[str, Any] | None = None,
        source_requery: Mapping[str, Any] | None = None,
        facts: Mapping[str, Any] | None = None,
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
            expected_revision=expected_revision or gap.expected_revision,
            installed_artifact=installed_artifact or gap.installed_artifact,
            source_requery=source_requery or gap.source_requery,
        )
        self._validate_authority(updated, to_state, actor_id, seat)
        self._validate_evidence(updated, to_state)
        self._validate_dependencies(updated, to_state)
        event_facts = dict(facts or {})
        if expected_revision is not None:
            event_facts["expected_revision"] = expected_revision
        if installed_artifact is not None:
            event_facts["installed_artifact"] = dict(installed_artifact)
        if source_requery is not None:
            event_facts["source_requery"] = dict(source_requery)
        previous_hash = self.events[-1].hash if self.events else "0" * 64
        payload = {
            "sequence": len(self.events) + 1,
            "gap_key": key,
            "from_state": gap.state,
            "to_state": to_state,
            "actor_id": actor_id,
            "seat": seat,
            "evidence": [item.as_dict() for item in evidence],
            "facts": event_facts,
        }
        event = LedgerEvent(
            sequence=payload["sequence"],
            gap_key=key,
            from_state=gap.state,
            to_state=to_state,
            actor_id=actor_id,
            seat=seat,
            evidence=tuple(evidence),
            facts=event_facts,
            previous_hash=previous_hash,
            hash=_event_hash(previous_hash, payload),
        )
        self.gaps[key] = replace(updated, state=to_state)
        self.events.append(event)
        return event

    def _validate_transition(self, gap: WorkGap, to_state: str) -> None:
        if gap.state in TERMINAL_STATES and to_state != "REGRESSED":
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
        if state == "DELIVERED":
            WorkGapLedger._validate_installed_delivery(gap)

    @staticmethod
    def _validate_installed_delivery(gap: WorkGap) -> None:
        if not gap.expected_revision:
            raise LedgerError("DELIVERED requires an expected revision")
        installed = gap.installed_artifact
        source = gap.source_requery
        if not isinstance(installed, Mapping):
            raise LedgerError("DELIVERED requires an installed artifact re-query")
        if not isinstance(source, Mapping):
            raise LedgerError("DELIVERED requires a source re-query")
        expected = gap.expected_revision
        if installed.get("expected_commit") != expected:
            raise LedgerError("installed artifact expected commit mismatch")
        if installed.get("installed_commit") != expected:
            raise LedgerError("installed artifact commit mismatch")
        if installed.get("match") is not True:
            raise LedgerError("installed artifact content does not match checkout")
        digest = str(installed.get("sha256") or "")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise LedgerError("installed artifact requires a sha256 digest")
        if source.get("commit") != expected:
            raise LedgerError("source re-query commit mismatch")
        if source.get("state") not in {"merged", "released", "closed"}:
            raise LedgerError("source re-query is not terminal")

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

    def regress(
        self, key: str, *, actor_id: str, evidence: Sequence[Evidence]
    ) -> tuple[LedgerEvent, ...]:
        """Append regression events for a gap and every transitive dependent."""
        affected = {key}
        changed = True
        while changed:
            changed = False
            for candidate in self.gaps.values():
                if candidate.key not in affected and any(
                    dependency in affected for dependency in candidate.dependencies
                ):
                    affected.add(candidate.key)
                    changed = True
        events = []
        for affected_key in sorted(affected):
            gap = self.gaps[affected_key]
            if gap.state == "REGRESSED":
                continue
            events.append(self.transition(
                affected_key, "REGRESSED", actor_id=actor_id,
                seat="completion", evidence=evidence,
                facts={"regressed_from": gap.state, "root_gap": key},
            ))
        return tuple(events)

    def explain(self, key: str) -> dict[str, Any]:
        gap = self._get(key)
        missing = [
            kind for kind in gap.expected_evidence
            if not any(item.kind == kind for item in gap.evidence)
        ]
        dependencies = {
            dependency: self._get(dependency).state for dependency in gap.dependencies
        }
        next_state = None
        if gap.state in PROGRESS_STATES and gap.state not in TERMINAL_STATES:
            next_state = PROGRESS_STATES[PROGRESS_STATES.index(gap.state) + 1]
        blockers = []
        if not gap.owner_project or not gap.owner_agent:
            blockers.append("owner_missing")
        blockers.extend(
            "dependency:%s:%s" % (dependency, state)
            for dependency, state in dependencies.items()
            if state not in TERMINAL_STATES
        )
        if gap.state == "REGRESSED":
            blockers.append("regression_open")
        if next_state == "DELIVERED":
            try:
                self._validate_installed_delivery(gap)
            except LedgerError as exc:
                blockers.append(str(exc))
        return {
            "schema": "simplicio.work-gap-explain/v1",
            "gap_key": key,
            "state": gap.state,
            "terminal": gap.state in TERMINAL_STATES,
            "next_state": next_state,
            "missing_evidence": missing,
            "dependencies": dependencies,
            "blockers": blockers,
            "event_sequences": [
                event.sequence for event in self.events if event.gap_key == key
            ],
            "ledger_digest": self.digest(),
        }


def validate_work_gap_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an immutable ledger snapshot before terminal completion."""
    errors: list[str] = []
    if not isinstance(snapshot, Mapping):
        return {"ok": False, "reason_code": "work_gap_ledger_invalid", "detail": {"errors": ["snapshot must be an object"]}}
    if snapshot.get("schema") != SCHEMA:
        errors.append("schema mismatch")
    gaps = snapshot.get("gaps")
    events = snapshot.get("events")
    if not isinstance(gaps, list) or not gaps:
        errors.append("gaps must be a non-empty list")
        gaps = []
    if not isinstance(events, list):
        errors.append("events must be a list")
        events = []

    delivered = set()
    gap_by_key: dict[str, Mapping[str, Any]] = {}
    for index, gap in enumerate(gaps):
        if not isinstance(gap, Mapping):
            errors.append(f"gap {index} is not an object")
            continue
        key = f"{gap.get('requirement_id', '')}:{gap.get('acceptance_criterion_id', '')}"
        if key in gap_by_key:
            errors.append(f"duplicate gap {key}")
        gap_by_key[key] = gap
        if gap.get("state") != "DELIVERED":
            errors.append(f"{key} is {gap.get('state')!r}, not DELIVERED")
        else:
            delivered.add(key)
        if not gap.get("owner_project") or not gap.get("owner_agent"):
            errors.append(f"{key} has no owner")
        identities = [gap.get("executor_id"), gap.get("verifier_id"), gap.get("completion_auditor_id")]
        if gap.get("state") == "DELIVERED" and (not all(identities) or len(set(identities)) != 3):
            errors.append(f"{key} lacks three independent terminal seats")
        for dependency in gap.get("dependencies") or []:
            if dependency not in delivered and not any(
                f"{item.get('requirement_id', '')}:{item.get('acceptance_criterion_id', '')}" == dependency
                for item in gaps if isinstance(item, Mapping)
            ):
                errors.append(f"{key} references an unknown dependency {dependency!r}")
        evidence = gap.get("evidence")
        if not isinstance(evidence, list):
            errors.append(f"{key} evidence must be a list")
            evidence = []
        kinds = set()
        for item in evidence:
            if not isinstance(item, Mapping):
                errors.append(f"{key} has malformed evidence")
                continue
            kinds.add(item.get("kind"))
            digest = str(item.get("digest") or "")
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                errors.append(f"{key} has invalid evidence digest")
        if gap.get("state") == "DELIVERED":
            for required in ("implementation", "verification", "integration", "delivery"):
                if required not in kinds:
                    errors.append(f"{key} lacks {required} evidence")
            try:
                WorkGapLedger._validate_installed_delivery(_gap_from_mapping(gap))
            except LedgerError as exc:
                errors.append(f"{key}: {exc}")

    # Detect cycles independently of current state. A cycle must be explicit,
    # even when every referenced key exists.
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str, trail: tuple[str, ...]) -> None:
        if key in visiting:
            errors.append("dependency cycle: " + " -> ".join(trail + (key,)))
            return
        if key in visited:
            return
        visiting.add(key)
        gap = gap_by_key.get(key, {})
        for dependency in gap.get("dependencies") or []:
            if dependency in gap_by_key:
                visit(str(dependency), trail + (key,))
        visiting.remove(key)
        visited.add(key)

    for key in sorted(gap_by_key):
        visit(key, ())
    for key, gap in gap_by_key.items():
        for dependency in gap.get("dependencies") or []:
            dependency_gap = gap_by_key.get(str(dependency))
            if (
                gap.get("state") in {"INTEGRATED", "DELIVERED"}
                and dependency_gap is not None
                and dependency_gap.get("state") not in TERMINAL_STATES
            ):
                errors.append(
                    f"{key} depends on non-terminal {dependency}: "
                    f"{dependency_gap.get('state')}"
                )

    previous = "0" * 64
    replay_states = {key: "UNMAPPED" for key in gap_by_key}
    replay_evidence: dict[str, list[Mapping[str, Any]]] = {
        key: [] for key in gap_by_key
    }
    for expected_sequence, event in enumerate(events, 1):
        if not isinstance(event, Mapping):
            errors.append(f"event {expected_sequence} is not an object")
            continue
        payload_names = [
            "sequence", "gap_key", "from_state", "to_state", "actor_id", "seat", "evidence"
        ]
        if "facts" in event:
            payload_names.append("facts")
        payload = {name: event.get(name) for name in payload_names}
        if event.get("sequence") != expected_sequence or event.get("previous_hash") != previous:
            errors.append(f"event {expected_sequence} sequence/link mismatch")
        if event.get("hash") != _event_hash(previous, payload):
            errors.append(f"event {expected_sequence} hash mismatch")
        previous = str(event.get("hash") or previous)
        key = str(event.get("gap_key") or "")
        if key not in gap_by_key:
            errors.append(f"event {expected_sequence} references unknown gap {key!r}")
            continue
        current = replay_states[key]
        if event.get("from_state") != current:
            errors.append(
                f"event {expected_sequence} replay state mismatch: "
                f"{event.get('from_state')!r} != {current!r}"
            )
        target = str(event.get("to_state") or "")
        legal = False
        if target in EXCEPTION_STATES:
            legal = not (target == "N_A_APPROVED" and current == "UNMAPPED")
        elif current in TERMINAL_STATES:
            legal = target == "REGRESSED"
        elif current in EXCEPTION_STATES:
            legal = target in {"UNMAPPED", "OWNED"}
        elif current in PROGRESS_STATES:
            index = PROGRESS_STATES.index(current)
            legal = index + 1 < len(PROGRESS_STATES) and PROGRESS_STATES[index + 1] == target
        if not legal:
            errors.append(f"event {expected_sequence} illegal transition {current} -> {target}")
        replay_states[key] = target
        evidence_rows = event.get("evidence")
        if isinstance(evidence_rows, list):
            replay_evidence[key].extend(
                item for item in evidence_rows if isinstance(item, Mapping)
            )
        gap = gap_by_key[key]
        seat = event.get("seat")
        actor = event.get("actor_id")
        to_state = event.get("to_state")
        expected_actor = {
            "IMPLEMENTED": ("executor", gap.get("executor_id")),
            "VERIFIED": ("verifier", gap.get("verifier_id")),
            "DELIVERED": ("completion", gap.get("completion_auditor_id")),
        }.get(to_state)
        if expected_actor and (seat, actor) != expected_actor:
            errors.append(f"event {expected_sequence} has invalid authority")

    for key, gap in gap_by_key.items():
        if replay_states[key] != gap.get("state"):
            errors.append(
                f"{key} replay ended at {replay_states[key]!r}, "
                f"snapshot claims {gap.get('state')!r}"
            )
        declared_evidence = gap.get("evidence") or []
        if replay_evidence[key] != declared_evidence:
            errors.append(f"{key} replay evidence differs from snapshot")

    detail = {
        "errors": errors,
        "gap_count": len(gaps),
        "event_count": len(events),
        "unresolved": [
            f"{gap.get('requirement_id', '')}:{gap.get('acceptance_criterion_id', '')}"
            for gap in gaps if isinstance(gap, Mapping) and gap.get("state") != "DELIVERED"
        ],
    }
    detail["digest"] = hashlib.sha256(_canonical(snapshot)).hexdigest()
    return {
        "ok": not errors,
        "reason_code": "work_gap_ledger_ready" if not errors else "work_gap_ledger_invalid",
        "detail": detail,
    }


def _gap_from_mapping(value: Mapping[str, Any]) -> WorkGap:
    evidence = tuple(
        Evidence(
            str(item.get("kind") or ""),
            str(item.get("handle") or ""),
            str(item.get("digest") or ""),
            str(item.get("actor_id") or ""),
        )
        for item in value.get("evidence") or []
        if isinstance(item, Mapping)
    )
    return WorkGap(
        requirement_id=str(value.get("requirement_id") or ""),
        acceptance_criterion_id=str(value.get("acceptance_criterion_id") or ""),
        state=str(value.get("state") or "UNMAPPED"),
        owner_project=value.get("owner_project"),
        owner_agent=value.get("owner_agent"),
        dependencies=tuple(value.get("dependencies") or ()),
        expected_evidence=tuple(value.get("expected_evidence") or ()),
        delivery_target=value.get("delivery_target"),
        executor_id=value.get("executor_id"),
        verifier_id=value.get("verifier_id"),
        completion_auditor_id=value.get("completion_auditor_id"),
        evidence=evidence,
        revision=int(value.get("revision") or 0),
        expected_revision=value.get("expected_revision"),
        installed_artifact=value.get("installed_artifact"),
        source_requery=value.get("source_requery"),
    )

def sha256_evidence(kind: str, handle: str, content: bytes, actor_id: str) -> Evidence:
    return Evidence(kind, handle, hashlib.sha256(content).hexdigest(), actor_id)
