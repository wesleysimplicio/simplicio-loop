"""Evidence-based semantic convergence and anti-oscillation controller."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

SCHEMA = "simplicio.progress-signal/v1"
CONTROLLER_SCHEMA = "simplicio.semantic-convergence/v1"


class ControllerState(str, Enum):
    PROGRESSING = "PROGRESSING"
    STALLED = "STALLED"
    REPLAN = "REPLAN"
    REROUTE = "REROUTE"
    ESCALATE = "ESCALATE"
    DRAIN = "DRAIN"
    WAITING = "WAITING"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    VERIFIED = "VERIFIED"


TERMINAL_STATES = frozenset({ControllerState.BLOCKED, ControllerState.FAILED, ControllerState.VERIFIED})
_ALLOWED = {
    ControllerState.PROGRESSING: {ControllerState.PROGRESSING, ControllerState.STALLED, ControllerState.WAITING, ControllerState.BLOCKED, ControllerState.FAILED, ControllerState.VERIFIED},
    ControllerState.STALLED: {ControllerState.STALLED, ControllerState.REPLAN, ControllerState.WAITING, ControllerState.BLOCKED, ControllerState.FAILED, ControllerState.VERIFIED},
    ControllerState.REPLAN: {ControllerState.REPLAN, ControllerState.REROUTE, ControllerState.BLOCKED, ControllerState.FAILED, ControllerState.VERIFIED},
    ControllerState.REROUTE: {ControllerState.REROUTE, ControllerState.ESCALATE, ControllerState.BLOCKED, ControllerState.FAILED, ControllerState.VERIFIED},
    ControllerState.ESCALATE: {ControllerState.ESCALATE, ControllerState.DRAIN, ControllerState.BLOCKED, ControllerState.FAILED, ControllerState.VERIFIED},
    ControllerState.DRAIN: {ControllerState.DRAIN, ControllerState.PROGRESSING, ControllerState.STALLED, ControllerState.BLOCKED, ControllerState.FAILED, ControllerState.VERIFIED},
    ControllerState.WAITING: {ControllerState.WAITING, ControllerState.PROGRESSING, ControllerState.STALLED, ControllerState.BLOCKED, ControllerState.FAILED, ControllerState.VERIFIED},
    ControllerState.BLOCKED: {ControllerState.BLOCKED},
    ControllerState.FAILED: {ControllerState.FAILED},
    ControllerState.VERIFIED: {ControllerState.VERIFIED},
}

_SEMANTIC_KEYS = (
    "task_frontier", "files_hash", "diff_hash", "tree_hash",
    "validation", "receipts", "blockers", "tool_result",
    "user_input", "delivery", "external_state",
)


class SignalError(ValueError):
    pass


class TransitionError(ValueError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _semantic_values(raw: Mapping[str, Any]) -> dict[str, Any]:
    source = raw.get("semantic") or raw.get("changes") or {}
    if not isinstance(source, Mapping):
        raise SignalError("semantic changes must be a mapping, not prose")
    return {key: source[key] for key in _SEMANTIC_KEYS if key in source}


@dataclass(frozen=True)
class ProgressSignal:
    signal_id: str
    source: str
    evidence_id: str
    evidence_hash: str
    semantic: dict[str, Any]
    strategy_hash: str = ""
    outcome: str = "progress"
    action: str = ""
    wait: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "signal_id": self.signal_id, "source": self.source,
                "evidence": {"id": self.evidence_id, "hash": self.evidence_hash},
                "semantic": dict(self.semantic), "strategy_hash": self.strategy_hash,
                "outcome": self.outcome, "action": self.action, "wait": dict(self.wait)}


@dataclass(frozen=True)
class EvidenceSnapshot:
    evidence_id: str
    evidence_hash: str
    semantic: dict[str, Any]
    acceptance_verified: bool = False
    delivery_verified: bool = False
    external_changed: bool = False

    @property
    def semantic_hash(self) -> str:
        return _digest(self.semantic)


def normalize_signal(raw: Mapping[str, Any]) -> ProgressSignal:
    if not isinstance(raw, Mapping) or raw.get("schema") != SCHEMA:
        raise SignalError("signal schema must be simplicio.progress-signal/v1")
    signal_id = str(raw.get("signal_id") or "").strip()
    source = str(raw.get("source") or "").strip()
    if not signal_id or source not in {"agent", "loop"}:
        raise SignalError("signal_id and source (agent or loop) are required")
    evidence = raw.get("evidence")
    if not isinstance(evidence, Mapping):
        raise SignalError("typed signal requires evidence id/hash")
    evidence_id = str(evidence.get("id") or "").strip()
    evidence_hash = str(evidence.get("hash") or "").strip()
    if not evidence_id and not evidence_hash:
        raise SignalError("typed signal requires a concrete evidence id or hash")
    semantic = _semantic_values(raw)
    wait = raw.get("wait") or {}
    if not isinstance(wait, Mapping):
        raise SignalError("wait must be a mapping")
    return ProgressSignal(signal_id, source, evidence_id or evidence_hash,
                          evidence_hash or _digest(evidence), semantic,
                          str(raw.get("strategy_hash") or ""),
                          str(raw.get("outcome") or "progress").lower(),
                          str(raw.get("action") or "").lower(), dict(wait))


def merge_evidence(signal: ProgressSignal, loop_evidence: Mapping[str, Any] | None = None) -> EvidenceSnapshot:
    loop = dict(loop_evidence or {})
    semantic = dict(signal.semantic)
    extra = loop.get("semantic") or loop.get("changes") or {}
    if isinstance(extra, Mapping):
        semantic.update({key: extra[key] for key in _SEMANTIC_KEYS if key in extra})
    evidence_id = str(loop.get("evidence_id") or loop.get("evidence_hash") or signal.evidence_id)
    evidence_hash = str(loop.get("evidence_hash") or signal.evidence_hash or _digest(semantic))
    return EvidenceSnapshot(evidence_id, evidence_hash, semantic,
                            bool(loop.get("acceptance_verified")),
                            bool(loop.get("delivery_verified")),
                            bool(loop.get("external_changed")))


@dataclass
class ConvergenceController:
    state: ControllerState = ControllerState.PROGRESSING
    stall_window: int = 0
    replans: int = 0
    reroutes: int = 0
    escalations: int = 0
    attempts: int = 0
    max_replans: int = 3
    max_reroutes: int = 3
    max_escalations: int = 2
    max_attempts: int = 20
    last_semantic_hash: str = ""
    last_strategy_hash: str = ""
    transitions: list[dict[str, Any]] = field(default_factory=list)

    def transition(self, target: ControllerState, evidence: EvidenceSnapshot, reason: str) -> dict[str, Any]:
        if not evidence.evidence_id and not evidence.evidence_hash:
            raise TransitionError("every transition needs concrete evidence")
        if target not in _ALLOWED[self.state]:
            raise TransitionError(f"invalid transition {self.state.value}->{target.value}")
        row = {"from": self.state.value, "to": target.value, "reason": reason,
               "evidence_id": evidence.evidence_id, "evidence_hash": evidence.evidence_hash}
        self.state = target
        self.transitions.append(row)
        return row

    def _receipt(self, evidence: EvidenceSnapshot, transition: dict[str, Any], reason: str, semantic_changed: bool) -> dict[str, Any]:
        return {"schema": CONTROLLER_SCHEMA, "state": self.state.value, "reason": reason,
                "semantic_changed": semantic_changed, "stall_window": self.stall_window,
                "counters": {"replans": self.replans, "reroutes": self.reroutes,
                             "escalations": self.escalations, "attempts": self.attempts},
                "transition": transition, "evidence_id": evidence.evidence_id,
                "evidence_hash": evidence.evidence_hash}

    def step(self, raw_signal: Mapping[str, Any], loop_evidence: Mapping[str, Any] | None = None, *, now: float | None = None) -> dict[str, Any]:
        raw_id = str(raw_signal.get("signal_id") or "invalid") if isinstance(raw_signal, Mapping) else "invalid"
        try:
            signal = normalize_signal(raw_signal)
            evidence = merge_evidence(signal, loop_evidence)
        except SignalError as exc:
            evidence = EvidenceSnapshot(raw_id, _digest(raw_signal), {})
            self.state = ControllerState.FAILED
            row = {"from": self.state.value, "to": self.state.value, "reason": f"invalid_signal:{exc}",
                   "evidence_id": evidence.evidence_id, "evidence_hash": evidence.evidence_hash}
            self.transitions.append(row)
            return self._receipt(evidence, row, row["reason"], False)
        if self.state in TERMINAL_STATES:
            row = self.transition(self.state, evidence, "terminal_state_preserved")
            return self._receipt(evidence, row, "terminal_state_preserved", False)
        semantic_changed = evidence.semantic_hash != self.last_semantic_hash
        self.last_semantic_hash = evidence.semantic_hash
        self.attempts += 1
        if self.attempts > self.max_attempts:
            row = self.transition(ControllerState.BLOCKED, evidence, "attempt_cap_exhausted")
            return self._receipt(evidence, row, "attempt_cap_exhausted", semantic_changed)
        if signal.outcome == "blocked":
            row = self.transition(ControllerState.BLOCKED, evidence, "explicit_blocker")
            return self._receipt(evidence, row, "explicit_blocker", semantic_changed)
        if signal.outcome == "failed":
            row = self.transition(ControllerState.FAILED, evidence, "explicit_failure")
            return self._receipt(evidence, row, "explicit_failure", semantic_changed)
        if signal.outcome == "verified":
            if not (evidence.acceptance_verified and evidence.delivery_verified):
                reason = "verified_requires_acceptance_and_delivery_evidence"
                row = self.transition(ControllerState.STALLED, evidence, reason)
                return self._receipt(evidence, row, reason, semantic_changed)
            row = self.transition(ControllerState.VERIFIED, evidence, "acceptance_and_delivery_verified")
            return self._receipt(evidence, row, "acceptance_and_delivery_verified", semantic_changed)
        if signal.outcome == "waiting":
            wait = signal.wait
            if not wait.get("condition") or wait.get("heartbeat") is None or wait.get("deadline") is None:
                row = self.transition(ControllerState.FAILED, evidence, "invalid_wait_contract")
                return self._receipt(evidence, row, "invalid_wait_contract", semantic_changed)
            if now is not None and float(wait["deadline"]) <= now:
                row = self.transition(ControllerState.BLOCKED, evidence, "wait_deadline_expired")
                return self._receipt(evidence, row, "wait_deadline_expired", semantic_changed)
            row = self.transition(ControllerState.WAITING, evidence, "explicit_wait")
            return self._receipt(evidence, row, "explicit_wait", semantic_changed)
        action = signal.action
        if self.state == ControllerState.PROGRESSING:
            target, reason = (ControllerState.PROGRESSING, "semantic_progress") if semantic_changed else (ControllerState.STALLED, "semantic_stall")
        elif self.state == ControllerState.STALLED:
            strategy_changed = bool(signal.strategy_hash and signal.strategy_hash != self.last_strategy_hash)
            if action == "replan" and strategy_changed and self.replans < self.max_replans:
                self.replans += 1; self.last_strategy_hash = signal.strategy_hash
                target, reason = ControllerState.REPLAN, "strategy_changed"
            else:
                target, reason = ControllerState.STALLED, "replan_requires_strategy_delta"
        elif self.state == ControllerState.REPLAN:
            if action == "reroute" and self.reroutes < self.max_reroutes:
                self.reroutes += 1; target, reason = ControllerState.REROUTE, "bounded_reroute"
            else: target, reason = ControllerState.REPLAN, "awaiting_reroute"
        elif self.state == ControllerState.REROUTE:
            if action == "escalate" and self.escalations < self.max_escalations:
                self.escalations += 1; target, reason = ControllerState.ESCALATE, "bounded_escalation"
            else: target, reason = ControllerState.REROUTE, "awaiting_escalation"
        elif self.state == ControllerState.ESCALATE:
            target, reason = (ControllerState.DRAIN, "drain_after_escalation") if action == "drain" else (ControllerState.ESCALATE, "awaiting_drain")
        elif self.state == ControllerState.DRAIN:
            target, reason = (ControllerState.PROGRESSING, "drain_made_progress") if semantic_changed else (ControllerState.STALLED, "drain_stalled")
        else:
            target, reason = (ControllerState.PROGRESSING, "external_state_changed") if evidence.external_changed or semantic_changed else (ControllerState.STALLED, "no_observable_delta")
        if target == ControllerState.STALLED: self.stall_window += 1
        else: self.stall_window = 0
        row = self.transition(target, evidence, reason)
        return self._receipt(evidence, row, reason, semantic_changed)


__all__ = ["CONTROLLER_SCHEMA", "SCHEMA", "ControllerState", "ConvergenceController", "EvidenceSnapshot", "ProgressSignal", "SignalError", "TransitionError", "merge_evidence", "normalize_signal"]
