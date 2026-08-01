"""Thin, action-gated bridge for the native tasks pipeline."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping

from .hub_governor import ResourceGovernor, ResourceLimits, ResourceRequest, ResourceThrottled
from .runner import dispatch_operator_batch

SCHEMA = "simplicio.tasks-orchestrator/v1"

def _digest(value: Any) -> str:
    blob = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()

def _evidence_complete(rows: Any) -> bool:
    return bool(rows) and all(row.get("pr") and row.get("verification") for row in rows)

class TasksOrchestrator:
    """Join existing intake, dispatcher and stage/review/delivery coordinator."""

    def __init__(
        self,
        intake: Any,
        contract_factory: Callable[[Mapping[str, Any]], list[Mapping[str, Any]]],
        coordinate: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        *,
        dispatch: Callable[..., Mapping[str, Any]] = dispatch_operator_batch,
        governor: ResourceGovernor | None = None,
        worktree_queue: Any = None,
        max_workers: int = 1,
        retry_budget: int = 1,
        journal_dir: str | None = None,
    ):
        self.intake = intake
        self.contract_factory = contract_factory
        self.dispatch = dispatch
        self.coordinate = coordinate
        self.max_workers = max(1, int(max_workers))
        self.retry_budget = max(0, int(retry_budget))
        self.journal_dir = journal_dir
        self.worktree_queue = worktree_queue
        self.governor = governor or ResourceGovernor(ResourceLimits(processes=self.max_workers))

    def run(self, request: str, *, action_gate: bool = False, cancel: bool = False) -> dict[str, Any]:
        plan = self.intake.run(request)
        identity = plan.get("run_identity", {})
        key = _digest(identity or plan.get("digests", {}))
        receipt = {"schema": SCHEMA, "plan": plan, "idempotency_key": key}
        if cancel:
            cancel_all = getattr(self.coordinate, "cancel_all", None)
            cancelled = cancel_all(reason="cancel_requested") if cancel_all else []
            receipt.update(state="cancelled", reason="cancel_requested", cancelled=cancelled, evidence=[])
            return receipt
        outcome = plan.get("outcome", {})
        if outcome.get("status") != "PLANNED_NOT_EXECUTED":
            receipt.update(state="blocked", reason=outcome.get("reason_code", "intake_not_planned"), evidence=[])
            return receipt
        if not action_gate:
            receipt.update(state="partial", reason="action_gate_required", evidence=[])
            return receipt
        items = self.contract_factory(plan)
        if not items:
            receipt.update(state="blocked", reason="no_validated_loop_contracts", evidence=[])
            return receipt
        requested = min(self.max_workers, len(items))
        try:
            lease = self.governor.admit("simplicio-tasks", key, ResourceRequest(processes=requested), lease_id=key)
        except ResourceThrottled as exc:
            receipt.update(state="blocked", reason="governor_throttled", governor=exc.receipt, evidence=[])
            return receipt
        try:
            dispatched = self.dispatch(items, max_workers=requested, retry_budget=self.retry_budget, journal_dir=self.journal_dir, worktree_queue=self.worktree_queue)
            coordinated = self.coordinate(dispatched)
        finally:
            release = self.governor.release(lease)
        evidence = coordinated.get("evidence", [])
        passed = bool(coordinated.get("passed")) and _evidence_complete(evidence)
        receipt.update(state="completed" if passed else "partial", reason="verified" if passed else "evidence_incomplete", dispatch=dispatched, evidence=evidence, review=coordinated, governor_release=release)
        return receipt
