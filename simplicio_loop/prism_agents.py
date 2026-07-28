"""Accountable agent ownership for Prism task transitions."""

from __future__ import annotations

import dataclasses
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .hbp_ledger import canonical_sha256
from .prism_contracts import TASK_STATES, TaskOwnership

ASSIGNMENT_SCHEMA = "simplicio.prism-agent-assignment/v1"
MESSAGE_SCHEMA = "simplicio.prism-agent-message/v1"
RECEIPT_SCHEMA = "simplicio.prism-agent-receipt/v1"
INDEPENDENT_ROLES = frozenset({"review", "completion"})


class PrismAgentError(RuntimeError):
    reason_code = "PRISM_AGENT_ERROR"


@dataclass(frozen=True)
class AgentDescriptor:
    agent_id: str
    host: str
    capabilities: tuple[str, ...]
    roles: tuple[str, ...]
    max_inbox: int = 32
    max_outbox: int = 32

    def __post_init__(self) -> None:
        for name in ("agent_id", "host"):
            if not str(getattr(self, name)).strip():
                raise PrismAgentError(f"{name} is required")
        object.__setattr__(
            self,
            "capabilities",
            tuple(
                sorted(
                    {
                        str(item).strip()
                        for item in self.capabilities
                        if str(item).strip()
                    }
                )
            ),
        )
        object.__setattr__(
            self,
            "roles",
            tuple(
                sorted({str(item).strip() for item in self.roles if str(item).strip()})
            ),
        )
        if not self.capabilities or not self.roles:
            raise PrismAgentError("agent capabilities and roles are required")
        if min(self.max_inbox, self.max_outbox) < 1:
            raise PrismAgentError("mailbox capacities must be positive")


@dataclass(frozen=True)
class AgentAssignment:
    prism_id: str
    slot_id: str
    task_id: str
    transition: str
    role: str
    agent_id: str
    host: str
    attempt: int
    fence: int
    lease_id: str
    lease_expires_ns: int
    accountable: bool = True
    helper_ids: tuple[str, ...] = ()
    previous_assignment_hash: str | None = None
    schema: str = ASSIGNMENT_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "prism_id",
            "slot_id",
            "task_id",
            "transition",
            "role",
            "agent_id",
            "host",
            "lease_id",
        ):
            if not str(getattr(self, name)).strip():
                raise PrismAgentError(f"{name} is required")
        if self.schema != ASSIGNMENT_SCHEMA:
            raise PrismAgentError("unknown assignment schema")
        if self.transition not in TASK_STATES:
            raise PrismAgentError("unknown transition")
        if self.attempt < 1 or self.fence < 1 or self.lease_expires_ns < 1:
            raise PrismAgentError("attempt, fence, and expiry must be positive")
        object.__setattr__(self, "helper_ids", tuple(sorted(set(self.helper_ids))))

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @property
    def assignment_hash(self) -> str:
        return canonical_sha256(self.to_dict())


@dataclass(frozen=True)
class AgentMessage:
    prism_id: str
    slot_id: str
    task_id: str
    attempt: int
    fence: int
    sender_id: str
    recipient_id: str
    transition: str
    payload_hash: str
    sequence: int
    schema: str = MESSAGE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MESSAGE_SCHEMA:
            raise PrismAgentError("unknown message schema")
        if min(self.attempt, self.fence, self.sequence) < 1:
            raise PrismAgentError("attempt, fence, and sequence must be positive")
        if len(self.payload_hash) != 64:
            raise PrismAgentError("payload_hash must be SHA-256")

    def to_dict(self) -> dict[str, Any]:
        payload = dataclasses.asdict(self)
        payload["message_hash"] = canonical_sha256(payload)
        return payload


class PrismAgentRegistry:
    """Data-driven assignment, bounded mailboxes, heartbeat, and takeover."""

    def __init__(
        self,
        *,
        clock_ns: Callable[[], int] = time.time_ns,
        default_ttl_ns: int = 30_000_000_000,
    ) -> None:
        if default_ttl_ns < 1:
            raise PrismAgentError("default_ttl_ns must be positive")
        self.clock_ns = clock_ns
        self.default_ttl_ns = default_ttl_ns
        self.agents: dict[str, AgentDescriptor] = {}
        self.assignments: dict[tuple[str, str], AgentAssignment] = {}
        self.history: list[dict[str, Any]] = []
        self.inboxes: dict[str, deque[dict[str, Any]]] = {}
        self.outboxes: dict[str, deque[dict[str, Any]]] = {}
        self._sequence = 0
        self._metrics = {
            "assignments": 0,
            "heartbeats": 0,
            "takeovers": 0,
            "messages": 0,
            "queue_overflow": 0,
            "stale_rejections": 0,
        }

    def register(self, agent: AgentDescriptor) -> None:
        if agent.agent_id in self.agents:
            raise PrismAgentError("duplicate agent")
        self.agents[agent.agent_id] = agent
        self.inboxes[agent.agent_id] = deque(maxlen=agent.max_inbox)
        self.outboxes[agent.agent_id] = deque(maxlen=agent.max_outbox)

    def _eligible(
        self,
        *,
        capability: str,
        role: str,
        exclude: set[str],
    ) -> list[AgentDescriptor]:
        candidates = [
            item
            for item in self.agents.values()
            if capability in item.capabilities
            and role in item.roles
            and item.agent_id not in exclude
        ]
        load = {
            agent.agent_id: sum(
                assignment.agent_id == agent.agent_id
                for assignment in self.assignments.values()
            )
            for agent in candidates
        }
        return sorted(candidates, key=lambda item: (load[item.agent_id], item.agent_id))

    def assign(
        self,
        ownership: TaskOwnership,
        *,
        prism_id: str,
        transition: str,
        capability: str,
        role: str,
        implementer_id: str | None = None,
        helper_ids: tuple[str, ...] = (),
        ttl_ns: int | None = None,
    ) -> AgentAssignment:
        key = (ownership.task_id, transition)
        if key in self.assignments:
            raise PrismAgentError("transition already has an accountable owner")
        exclude = (
            {implementer_id} if implementer_id and role in INDEPENDENT_ROLES else set()
        )
        candidates = self._eligible(capability=capability, role=role, exclude=exclude)
        if not candidates:
            raise PrismAgentError("CAPABILITY_MISSING_OR_INDEPENDENCE_UNAVAILABLE")
        owner = candidates[0]
        now = int(self.clock_ns())
        ttl = self.default_ttl_ns if ttl_ns is None else int(ttl_ns)
        if ttl < 1:
            raise PrismAgentError("ttl_ns must be positive")
        assignment = AgentAssignment(
            prism_id=str(prism_id),
            slot_id=ownership.slot_id,
            task_id=ownership.task_id,
            transition=transition,
            role=role,
            agent_id=owner.agent_id,
            host=owner.host,
            attempt=ownership.attempt,
            fence=ownership.fence,
            lease_id=ownership.lease_id,
            lease_expires_ns=now + ttl,
            helper_ids=helper_ids,
        )
        self.assignments[key] = assignment
        self.history.append(
            assignment.to_dict() | {"assignment_hash": assignment.assignment_hash}
        )
        self._metrics["assignments"] += 1
        return assignment

    def heartbeat(
        self,
        task_id: str,
        transition: str,
        *,
        agent_id: str,
        fence: int,
        ttl_ns: int | None = None,
    ) -> AgentAssignment:
        key = (task_id, transition)
        current = self.assignments.get(key)
        if current is None:
            raise PrismAgentError("assignment missing")
        if current.agent_id != agent_id or current.fence != int(fence):
            self._metrics["stale_rejections"] += 1
            raise PrismAgentError("stale heartbeat")
        ttl = self.default_ttl_ns if ttl_ns is None else int(ttl_ns)
        if ttl < 1:
            raise PrismAgentError("ttl_ns must be positive")
        updated = replace(current, lease_expires_ns=int(self.clock_ns()) + ttl)
        self.assignments[key] = updated
        self._metrics["heartbeats"] += 1
        return updated

    def takeover(
        self,
        task_id: str,
        transition: str,
        *,
        capability: str,
        now_ns: int | None = None,
    ) -> AgentAssignment:
        key = (task_id, transition)
        current = self.assignments.get(key)
        if current is None:
            raise PrismAgentError("assignment missing")
        now = int(self.clock_ns()) if now_ns is None else int(now_ns)
        if now <= current.lease_expires_ns:
            raise PrismAgentError("lease is still active")
        candidates = self._eligible(
            capability=capability,
            role=current.role,
            exclude={current.agent_id},
        )
        if not candidates:
            raise PrismAgentError("takeover capability missing")
        owner = candidates[0]
        updated = replace(
            current,
            agent_id=owner.agent_id,
            host=owner.host,
            attempt=current.attempt + 1,
            fence=current.fence + 1,
            lease_id=f"{current.lease_id}:takeover:{current.attempt + 1}",
            lease_expires_ns=now + self.default_ttl_ns,
            previous_assignment_hash=current.assignment_hash,
        )
        self.assignments[key] = updated
        self.history.append(
            updated.to_dict() | {"assignment_hash": updated.assignment_hash}
        )
        self._metrics["takeovers"] += 1
        return updated

    def send(
        self,
        assignment: AgentAssignment,
        *,
        sender_id: str,
        recipient_id: str,
        payload: Mapping[str, Any],
    ) -> AgentMessage:
        current = self.assignments.get((assignment.task_id, assignment.transition))
        if current is None or current.assignment_hash != assignment.assignment_hash:
            self._metrics["stale_rejections"] += 1
            raise PrismAgentError("stale assignment cannot send")
        if sender_id not in self.agents or recipient_id not in self.agents:
            raise PrismAgentError("unknown sender or recipient")
        inbox, outbox = self.inboxes[recipient_id], self.outboxes[sender_id]
        if len(inbox) >= inbox.maxlen or len(outbox) >= outbox.maxlen:
            self._metrics["queue_overflow"] += 1
            raise PrismAgentError("agent mailbox overflow")
        self._sequence += 1
        message = AgentMessage(
            assignment.prism_id,
            assignment.slot_id,
            assignment.task_id,
            assignment.attempt,
            assignment.fence,
            sender_id,
            recipient_id,
            assignment.transition,
            canonical_sha256(payload),
            self._sequence,
        )
        row = message.to_dict()
        outbox.append(row)
        inbox.append(row)
        self._metrics["messages"] += 1
        return message

    def receipt(
        self,
        assignment: AgentAssignment,
        *,
        signer_id: str,
        verdict: str,
        evidence_hashes: tuple[str, ...],
    ) -> dict[str, Any]:
        current = self.assignments.get((assignment.task_id, assignment.transition))
        if current is None or current.assignment_hash != assignment.assignment_hash:
            raise PrismAgentError("stale assignment cannot sign")
        if signer_id != current.agent_id:
            raise PrismAgentError("helper or non-owner cannot sign")
        if int(self.clock_ns()) > current.lease_expires_ns:
            raise PrismAgentError("expired assignment cannot sign")
        if verdict not in {"accepted", "failed", "blocked", "cancelled"}:
            raise PrismAgentError("unsupported agent verdict")
        if any(len(value) != 64 for value in evidence_hashes):
            raise PrismAgentError("evidence hashes must be SHA-256")
        payload = {
            "schema": RECEIPT_SCHEMA,
            "assignment_hash": current.assignment_hash,
            "agent_id": signer_id,
            "task_id": current.task_id,
            "transition": current.transition,
            "attempt": current.attempt,
            "fence": current.fence,
            "verdict": verdict,
            "evidence_hashes": sorted(set(evidence_hashes)),
        }
        payload["receipt_hash"] = canonical_sha256(payload)
        return payload

    def status(self) -> dict[str, Any]:
        payload = {
            "schema": "simplicio.prism-agent-status/v1",
            "agents": sorted(self.agents),
            "active_assignments": sorted(
                f"{task_id}:{transition}" for task_id, transition in self.assignments
            ),
            "queue_depth": {
                agent_id: {
                    "inbox": len(self.inboxes[agent_id]),
                    "outbox": len(self.outboxes[agent_id]),
                }
                for agent_id in sorted(self.agents)
            },
            "metrics": dict(self._metrics),
        }
        payload["digest"] = canonical_sha256(payload)
        return payload


__all__ = [
    "ASSIGNMENT_SCHEMA",
    "MESSAGE_SCHEMA",
    "RECEIPT_SCHEMA",
    "AgentAssignment",
    "AgentDescriptor",
    "AgentMessage",
    "PrismAgentError",
    "PrismAgentRegistry",
]
