"""Verified Agent Delivery protocol: Loop -> Runtime -> Execution Board.

The loop owns the phase transition and evidence decision.  The Runtime adapter is the
transport boundary, while the Execution Board is a deterministic read model.  Completion
is fail-closed: an agent cannot publish ``done`` until a fresh COMPLETE receipt and a
measured watcher result have both been recorded.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .execution_board import ExecutionBoard
from .phase_events import build_phase_event, phase_to_board_state
from .runtime_adapter import LoopRuntimeAdapter

SCHEMA = "simplicio.verified-agent-delivery/v1"

_PHASE_BOARD_EVENTS = {
    "intake": "created",
    "mapping": "mapped",
    "planning": "planned",
    "executing": "attempt_started",
    "validating": "validation_started",
    "watching": "watcher_started",
    "delivering": "delivery_started",
    "done": "completed",
    "partial": "delivery_partial",
    "blocked": "blocked",
    "cancelled": "cancelled",
    "awaiting_decision": "human_gate_blocked",
}


class VerifiedDeliveryError(ValueError):
    """Raised when a delivery cannot be proven or the protocol is misused."""


class VerifiedAgentDelivery:
    """Coordinate one agent's verified delivery through all three planes.

    ``runtime`` must be a negotiated :class:`LoopRuntimeAdapter`; ``board`` is the
    local/event-sourced projection.  The board never receives a cosmetic status: its
    events are derived from the same validated phase events sent to Runtime.
    """

    def __init__(self, *, runtime: LoopRuntimeAdapter, board: ExecutionBoard,
                 attempt_id: str, identity: Optional[Mapping[str, Any]] = None) -> None:
        if runtime.run_id != board.run_id:
            raise VerifiedDeliveryError("runtime and board run bindings must match")
        if not isinstance(attempt_id, str) or not attempt_id.strip():
            raise VerifiedDeliveryError("attempt_id is required")
        self.runtime = runtime
        self.board = board
        self.attempt_id = attempt_id.strip()
        self.identity = dict(identity or runtime.identity or {})
        self.phase: Optional[str] = None
        self.sequence = 0
        self.previous_event_id: Optional[str] = None
        self.evidence: Optional[Mapping[str, Any]] = None
        self.watcher_measured = False
        self.delivery: Optional[Mapping[str, Any]] = None

    def transition(self, to_phase: str, *, reason_code: str = "phase_transition",
                   payload: Optional[Mapping[str, Any]] = None,
                   event_id: Optional[str] = None) -> Dict[str, Any]:
        """Emit one validated Loop event, deliver it to Runtime, and project it locally."""
        event_id = event_id or "%s-%04d" % (self.attempt_id, self.sequence + 1)
        event = build_phase_event(
            run_id=self.runtime.run_id, work_item_id=self.runtime.work_item_id,
            attempt_id=self.attempt_id, actor=self.runtime.actor,
            cause=self.previous_event_id or "agent_delivery", causation_id=self.previous_event_id,
            sequence=self.sequence + 1, event_id=event_id, from_phase=self.phase,
            to_phase=to_phase, reason_code=reason_code, payload=payload,
        )
        runtime_receipt = self.runtime.emit_event(event)
        if runtime_receipt.get("status") not in {"DELIVERED", "BUFFERED", "STANDALONE"}:
            raise VerifiedDeliveryError("runtime rejected phase event")
        board_payload = dict(payload or {})
        board_payload.update({"phase_event_id": event_id, "attempt_id": self.attempt_id,
                              "board_state": phase_to_board_state(to_phase)})
        if to_phase == "intake":
            board_payload.setdefault("title", board_payload.get("title") or self.runtime.work_item_id)
        if to_phase == "executing":
            board_payload.setdefault("attempt_id", self.attempt_id)
        self.board.append(_PHASE_BOARD_EVENTS[to_phase], item_id=self.runtime.work_item_id,
                          payload=board_payload)
        self.phase, self.sequence, self.previous_event_id = to_phase, self.sequence + 1, event_id
        return {"event": event, "runtime": runtime_receipt, "board_state": phase_to_board_state(to_phase)}

    def record_evidence(self, receipt: Mapping[str, Any]) -> Dict[str, Any]:
        if receipt.get("ready") is not True or receipt.get("verdict") != "COMPLETE":
            raise VerifiedDeliveryError("evidence must be a fresh COMPLETE receipt")
        self.runtime.record_evidence(receipt)
        self.evidence = dict(receipt)
        return self.board.append("evidence_recorded", item_id=self.runtime.work_item_id,
                                 payload={"attempt_id": self.attempt_id, "verified": True,
                                          "receipt_id": receipt.get("receipt_id", "")})

    def record_watcher(self, *, match: bool, challenge: str) -> Dict[str, Any]:
        if not match or not isinstance(challenge, str) or not challenge.strip():
            raise VerifiedDeliveryError("watcher must be measured with a non-empty challenge")
        self.watcher_measured = True
        return self.board.append("watcher_passed", item_id=self.runtime.work_item_id,
                                 payload={"attempt_id": self.attempt_id, "match": True,
                                          "challenge": challenge.strip()})

    def record_delivery(self, delivery: Mapping[str, Any]) -> Dict[str, Any]:
        target = str(delivery.get("target") or "").strip()
        if not target:
            raise VerifiedDeliveryError("delivery target is required")
        merge_queue = dict(delivery.get("merge_queue") or {})
        if "merge_queue_receipt_sha" in delivery and "receipt_sha" not in merge_queue:
            merge_queue["receipt_sha"] = delivery.get("merge_queue_receipt_sha")
        if "merge_queue_status" in delivery and "status" not in merge_queue:
            merge_queue["status"] = delivery.get("merge_queue_status")
        if "merge_queue_branch" in delivery and "branch" not in merge_queue:
            merge_queue["branch"] = delivery.get("merge_queue_branch")
        if "merge_queue_worktree_path" in delivery and "worktree_path" not in merge_queue:
            merge_queue["worktree_path"] = delivery.get("merge_queue_worktree_path")
        payload: Dict[str, Any] = {"attempt_id": self.attempt_id, "target": target,
                                   "satisfied": bool(delivery.get("satisfied"))}
        if merge_queue:
            payload["merge_queue"] = merge_queue
        if "receipt_sha" in merge_queue:
            payload["merge_queue_receipt_sha"] = merge_queue.get("receipt_sha")
        if "status" in merge_queue:
            payload["merge_queue_status"] = merge_queue.get("status")
        if "branch" in merge_queue:
            payload["merge_queue_branch"] = merge_queue.get("branch")
        if "worktree_path" in merge_queue:
            payload["merge_queue_worktree_path"] = merge_queue.get("worktree_path")
        self.delivery = dict(payload)
        return self.board.append("delivery_recorded", item_id=self.runtime.work_item_id, payload=payload)

    def complete(self, receipt: Mapping[str, Any]) -> Dict[str, Any]:
        if self.evidence is None or not self.watcher_measured:
            raise VerifiedDeliveryError("completion requires evidence and measured watcher gates")
        if self.delivery is None or not self.delivery.get("satisfied"):
            raise VerifiedDeliveryError("completion requires recorded delivery convergence")
        merge_queue = dict(self.delivery.get("merge_queue") or {})
        merge_receipt = str(merge_queue.get("receipt_sha") or self.delivery.get("merge_queue_receipt_sha") or "").strip()
        merge_status = str(merge_queue.get("status") or self.delivery.get("merge_queue_status") or "").strip().lower()
        merge_branch = str(merge_queue.get("branch") or self.delivery.get("merge_queue_branch") or "").strip()
        merge_worktree = str(merge_queue.get("worktree_path") or self.delivery.get("merge_queue_worktree_path") or "").strip()
        if self.delivery.get("target") != "local-fixture" and not (merge_receipt and merge_status == "accepted"):
            raise VerifiedDeliveryError("external delivery requires merge-queue acceptance evidence")
        if self.delivery.get("target") != "local-fixture" and not (merge_branch and merge_worktree):
            raise VerifiedDeliveryError("external delivery requires merge-queue worktree/branch evidence")
        self.runtime.complete(receipt)
        result = self.transition("done", reason_code="verified_delivery", payload={
            "oracle": receipt.get("verdict"), "receipt_id": receipt.get("receipt_id", "")})
        result["status"] = "VERIFIED"
        result["schema"] = SCHEMA
        result["delivery"] = dict(self.delivery)
        return result


__all__ = ["SCHEMA", "VerifiedAgentDelivery", "VerifiedDeliveryError"]
