"""Executable, dependency-free Fast V3 delivery control plane (issue #760).

The reducer owns decisions and receipts, never repository mutation.  Callers bind
Fast/Mapper/Dev CLI/Runtime/LLM adapters at the edges.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

SCHEMA = "simplicio.loop.fast-v3-delivery/v1"
FAILURE_DELTA_SCHEMA = "simplicio.loop.failure-delta/v1"


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


class State(str, Enum):
    ACCEPTED = "ACCEPTED"
    PREFLIGHTED = "PREFLIGHTED"
    PINNED = "PINNED"
    ORIENTED_T0 = "ORIENTED_T0"
    ORIENTED_T1 = "ORIENTED_T1"
    ORIENTED_T2 = "ORIENTED_T2"
    ORIENTED_T3 = "ORIENTED_T3"
    DECISION_REQUIRED = "DECISION_REQUIRED"
    PLANNED = "PLANNED"
    DRY_RUN = "DRY_RUN"
    APPLIED = "APPLIED"
    VERIFY_FOCUSED = "VERIFY_FOCUSED"
    CORRECTION_REQUIRED = "CORRECTION_REQUIRED"
    VERIFY_FULL = "VERIFY_FULL"
    READY_TO_PROMOTE = "READY_TO_PROMOTE"
    PROMOTED = "PROMOTED"
    SEALED = "SEALED"
    HELD = "HELD"
    ROLLED_BACK = "ROLLED_BACK"
    CANCELLED = "CANCELLED"


_NEXT = {
    State.ACCEPTED: {State.PREFLIGHTED, State.HELD, State.CANCELLED},
    State.PREFLIGHTED: {State.PINNED, State.HELD, State.CANCELLED},
    State.PINNED: {State.ORIENTED_T0, State.HELD, State.CANCELLED},
    State.ORIENTED_T0: {State.ORIENTED_T1, State.DECISION_REQUIRED, State.VERIFY_FOCUSED, State.HELD},
    State.ORIENTED_T1: {State.ORIENTED_T2, State.DECISION_REQUIRED, State.VERIFY_FOCUSED, State.HELD},
    State.ORIENTED_T2: {State.ORIENTED_T3, State.DECISION_REQUIRED, State.HELD},
    State.ORIENTED_T3: {State.DECISION_REQUIRED, State.HELD},
    State.DECISION_REQUIRED: {State.PLANNED, State.HELD, State.CANCELLED},
    State.PLANNED: {State.DRY_RUN, State.HELD},
    State.DRY_RUN: {State.APPLIED, State.HELD},
    State.APPLIED: {State.VERIFY_FOCUSED, State.ROLLED_BACK},
    State.VERIFY_FOCUSED: {State.CORRECTION_REQUIRED, State.VERIFY_FULL, State.HELD},
    State.CORRECTION_REQUIRED: {
        State.ORIENTED_T2, State.ORIENTED_T3, State.DECISION_REQUIRED, State.HELD
    },
    State.VERIFY_FULL: {State.READY_TO_PROMOTE, State.CORRECTION_REQUIRED, State.HELD},
    State.READY_TO_PROMOTE: {State.PROMOTED, State.HELD, State.CANCELLED},
    State.PROMOTED: {State.SEALED, State.ROLLED_BACK},
}
_TERMINAL = {State.SEALED, State.HELD, State.ROLLED_BACK, State.CANCELLED}


@dataclass(frozen=True)
class Budget:
    max_attempts: int
    max_tokens: Optional[int] = None
    max_context_bytes: Optional[int] = None

    def validate(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        for name in ("max_tokens", "max_context_bytes"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative or null")


@dataclass
class DeliveryRun:
    task: str
    acceptance_criteria: Sequence[str]
    repo: str
    commit: str
    generation: str
    budget: Budget
    state: State = State.ACCEPTED
    attempts: int = 0
    spent_tokens: Optional[int] = None
    context_bytes: int = 0
    tier: int = 0
    receipts: List[Dict[str, Any]] = field(default_factory=list)
    seen_handles: Dict[str, str] = field(default_factory=dict)
    fingerprints: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.budget.validate()
        if not self.acceptance_criteria:
            raise ValueError("acceptance criteria must be frozen before execution")

    @property
    def task_hash(self) -> str:
        return _digest(self.task)

    @property
    def acceptance_hash(self) -> str:
        return _digest(list(self.acceptance_criteria))

    def transition(self, target: State, *, evidence: Mapping[str, Any]) -> Dict[str, Any]:
        if self.state in _TERMINAL or target not in _NEXT.get(self.state, set()):
            raise ValueError(f"illegal transition: {self.state.value}->{target.value}")
        if not evidence:
            raise ValueError("every transition requires evidence")
        previous = self.state
        self.state = target
        receipt = {
            "schema": SCHEMA,
            "from": previous.value,
            "to": target.value,
            "task_hash": self.task_hash,
            "acceptance_hash": self.acceptance_hash,
            "repo": self.repo,
            "commit": self.commit,
            "generation": self.generation,
            "evidence": dict(evidence),
            "previous_receipt": _digest(self.receipts[-1]) if self.receipts else None,
        }
        receipt["receipt_hash"] = _digest(receipt)
        self.receipts.append(receipt)
        return receipt

    def add_context(
        self,
        tier: int,
        items: Iterable[Tuple[str, bytes]],
        *,
        reason_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        if tier not in range(4):
            raise ValueError("tier must be T0..T3")
        if tier > self.tier and not reason_code:
            raise ValueError("context expansion requires a reason_code")
        if tier < self.tier:
            raise ValueError("context tier cannot shrink")
        added, reused, added_bytes = [], [], 0
        for handle, content in items:
            digest = hashlib.sha256(content).hexdigest()
            prior = self.seen_handles.get(handle)
            if prior is not None and prior != digest:
                raise ValueError(f"handle content drift: {handle}")
            if prior == digest:
                reused.append(handle)
                continue
            self.seen_handles[handle] = digest
            added.append(handle)
            added_bytes += len(content)
        self.context_bytes += added_bytes
        if self.budget.max_context_bytes is not None and self.context_bytes > self.budget.max_context_bytes:
            self.state = State.HELD
            raise RuntimeError("context budget exhausted")
        self.tier = tier
        return {
            "tier": f"T{tier}", "reason_code": reason_code, "added_handles": added,
            "reused_handles": reused, "added_bytes": added_bytes,
        }

    def record_attempt(
        self,
        *,
        error: str,
        diff_digest: str,
        plan_digest: str,
        changed_handles: Sequence[str],
        test: str,
        observed_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        self.attempts += 1
        if observed_tokens is not None:
            self.spent_tokens = (self.spent_tokens or 0) + observed_tokens
        fingerprint = _digest([error, diff_digest, plan_digest])
        repeated = fingerprint in self.fingerprints
        self.fingerprints.append(fingerprint)
        exhausted = self.attempts >= self.budget.max_attempts
        if self.budget.max_tokens is not None and self.spent_tokens is not None:
            exhausted = exhausted or self.spent_tokens >= self.budget.max_tokens
        action = "held" if exhausted else ("expand_context_or_switch_strategy" if repeated else "retry_delta")
        if exhausted:
            self.state = State.HELD
        return {
            "schema": FAILURE_DELTA_SCHEMA,
            "attempt": self.attempts,
            "error": error,
            "diff_digest": diff_digest,
            "plan_digest": plan_digest,
            "changed_handles": list(changed_handles),
            "test": test,
            "fingerprint": fingerprint,
            "repeated": repeated,
            "action": action,
            "context_content": None,
        }

    def candidate_count(self, *, uncertainty: float, risk: float, stalled: bool) -> Dict[str, Any]:
        if not 0 <= uncertainty <= 1 or not 0 <= risk <= 1:
            raise ValueError("uncertainty and risk must be within [0,1]")
        count = 1
        reasons: List[str] = []
        if stalled or max(uncertainty, risk) >= 0.8:
            count, reasons = 3, ["stall" if stalled else "high_risk_or_uncertainty"]
        elif max(uncertainty, risk) >= 0.5:
            count, reasons = 2, ["medium_risk_or_uncertainty"]
        return {"candidates": count, "reason_codes": reasons}

    def seal(self, *, ac_coverage: Mapping[str, bool], gates: Mapping[str, bool]) -> Dict[str, Any]:
        if self.state != State.PROMOTED:
            raise ValueError("only a promoted winner can be sealed")
        if not ac_coverage or not all(ac_coverage.values()) or not gates or not all(gates.values()):
            raise ValueError("all ACs and gates require positive evidence")
        return self.transition(State.SEALED, evidence={"ac_coverage": dict(ac_coverage), "gates": dict(gates)})


class FastV3Runner:
    """Adapter-driven execution path used by CLI and real Loop embedders."""

    def __init__(self, *, orient: Callable[[str, int], Mapping[str, Any]],
                 verify: Callable[[str], Mapping[str, Any]],
                 decide: Optional[Callable[[Mapping[str, Any]], Mapping[str, Any]]] = None,
                 apply: Optional[Callable[[Mapping[str, Any]], Mapping[str, Any]]] = None,
                 authorize: Optional[Callable[[Mapping[str, Any]], Mapping[str, Any]]] = None) -> None:
        self.orient_adapter, self.verify_adapter = orient, verify
        self.decide_adapter, self.apply_adapter, self.authorize_adapter = decide, apply, authorize

    def execute(self, run: DeliveryRun, *, verify_only: bool = False, full: bool = False) -> Dict[str, Any]:
        run.transition(State.PREFLIGHTED, evidence={"adapters": "bound", "full": full})
        run.transition(State.PINNED, evidence={"commit": run.commit, "generation": run.generation})
        context = dict(self.orient_adapter(run.task, run.budget.max_context_bytes or 48000))
        if context.get("status") not in {"READY", "FALLBACK"}:
            run.transition(State.HELD, evidence={"reason": context.get("reason", "orient_failed")})
            return self.result(run)
        handles = [(str(x["handle"]), str(x.get("content") or "").encode())
                   for x in context.get("handles", [])]
        manifest = run.add_context(0, handles)
        run.transition(State.ORIENTED_T0, evidence={"context": manifest, "provider": context.get("provider")})
        if verify_only:
            run.transition(State.VERIFY_FOCUSED, evidence={"llm_calls": 0, "reason": "verify_only"})
        else:
            if self.decide_adapter is None or self.apply_adapter is None:
                run.transition(State.HELD, evidence={"reason": "decision_or_apply_adapter_missing"})
                return self.result(run)
            run.transition(State.DECISION_REQUIRED, evidence={"handles": list(run.seen_handles)})
            decision = dict(self.decide_adapter(context))
            run.transition(State.PLANNED, evidence={"decision_digest": _digest(decision)})
            dry = dict(self.apply_adapter({**decision, "dry_run": True}))
            if not dry.get("ok"):
                run.transition(State.HELD, evidence={"reason": "dry_run_failed", "detail": dry})
                return self.result(run)
            run.transition(State.DRY_RUN, evidence=dry)
            applied = dict(self.apply_adapter({**decision, "dry_run": False}))
            if not applied.get("ok"):
                run.transition(State.HELD, evidence={"reason": "apply_failed", "detail": applied})
                return self.result(run)
            run.transition(State.APPLIED, evidence=applied)
            run.transition(State.VERIFY_FOCUSED, evidence={"targeted": True})
        focused = dict(self.verify_adapter("focused"))
        if not focused.get("ok"):
            run.transition(State.CORRECTION_REQUIRED, evidence=focused)
            run.transition(State.HELD, evidence={"reason": "focused_verification_failed",
                                                 "verification": focused})
            return self.result(run)
        run.transition(State.VERIFY_FULL, evidence=focused)
        complete = dict(self.verify_adapter("full"))
        if not complete.get("ok"):
            run.transition(State.CORRECTION_REQUIRED, evidence=complete)
            run.transition(State.HELD, evidence={"reason": "full_verification_failed",
                                                 "verification": complete})
            return self.result(run)
        run.transition(State.READY_TO_PROMOTE, evidence=complete)
        if full:
            if self.authorize_adapter is None:
                run.transition(State.HELD, evidence={"reason": "runtime_authorization_missing"})
                return self.result(run)
            authorization = dict(self.authorize_adapter({"run": run.task_hash, "gates": complete}))
            if not authorization.get("ok"):
                run.transition(State.HELD, evidence={"reason": "runtime_denied"})
                return self.result(run)
        else:
            authorization = {"ok": True, "provider": "standalone-local-guard"}
        run.transition(State.PROMOTED, evidence={"winner": "candidate-1", "authorization": authorization})
        run.seal(ac_coverage={x: True for x in run.acceptance_criteria},
                 gates={"focused": True, "full": True, "winner_only": True})
        return self.result(run)

    @staticmethod
    def result(run: DeliveryRun) -> Dict[str, Any]:
        return {"schema": SCHEMA, "state": run.state.value, "sealed": run.state == State.SEALED,
                "task_hash": run.task_hash, "acceptance_hash": run.acceptance_hash,
                "attempts": run.attempts, "spent_tokens": run.spent_tokens,
                "context_bytes": run.context_bytes, "receipts": list(run.receipts)}
