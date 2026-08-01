"""Small, receipt-backed adapter for Fast bindings and WorktreeQueue delivery."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Sequence

from .fast_task_bridge import FastTaskBinding

SCHEMA = "simplicio.loop.composed-fast-delivery/v1"


class ComposedDeliveryError(RuntimeError):
    """A candidate cannot safely enter composed delivery."""


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ComposedDeliveryReceipt:
    task_id: str
    base_generation: str
    overlay_generation: str
    candidate_order: int
    merge_status: str
    verification_status: str
    receipt_hash: str
    schema: str = SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "task_id": self.task_id,
                "base_generation": self.base_generation, "overlay_generation": self.overlay_generation,
                "candidate_order": self.candidate_order, "merge_status": self.merge_status,
                "verification_status": self.verification_status, "receipt_hash": self.receipt_hash}


class ComposedFastDelivery:
    """Connect already-created Fast bindings to the existing merge queue."""

    def __init__(self, queue: Any) -> None:
        self.queue = queue
        self._bindings: dict[str, FastTaskBinding] = {}

    def register_binding(self, binding: FastTaskBinding) -> None:
        prior = self._bindings.get(binding.task_id)
        if prior and prior != binding:
            raise ComposedDeliveryError("task already has a different Fast binding")
        if any(other.worktree_id == binding.worktree_id and other.task_id != binding.task_id
               for other in self._bindings.values()):
            raise ComposedDeliveryError("worktree overlay is already bound to another task")
        if self._bindings and any(other.base_generation != binding.base_generation
                                  for other in self._bindings.values()):
            raise ComposedDeliveryError("batch Fast base generation drifted")
        self._bindings[binding.task_id] = binding

    def enqueue_verified_candidate(self, task_id: str, *, commands: Sequence[Sequence[str]],
                                   candidate_order: int | None = None,
                                   suite: str = "composed") -> ComposedDeliveryReceipt:
        binding = self._bindings.get(task_id)
        if binding is None:
            raise ComposedDeliveryError("candidate has no registered Fast binding")
        candidate = self.queue.enqueue_merge(task_id)
        if candidate.get("status") != "queued":
            raise ComposedDeliveryError(f"candidate is not merge-queued: {candidate.get('status')}")
        verification = self.queue.run_composed_verification(task_id, commands, suite=suite)
        payload = {"schema": SCHEMA, "task_id": task_id,
                   "base_generation": binding.base_generation,
                   "overlay_generation": binding.overlay_generation,
                   "candidate_order": int(candidate_order if candidate_order is not None else len(self._bindings)),
                   "merge_status": str(candidate.get("status") or ""),
                   "verification_status": "accepted" if verification.get("passed") else "rejected",
                   "merge_receipt": candidate, "verification_receipt": verification}
        receipt_hash = _hash(payload)
        return ComposedDeliveryReceipt(task_id=task_id, base_generation=binding.base_generation,
                                       overlay_generation=binding.overlay_generation,
                                       candidate_order=payload["candidate_order"],
                                       merge_status=payload["merge_status"],
                                       verification_status=payload["verification_status"],
                                       receipt_hash=receipt_hash)


__all__ = ["SCHEMA", "ComposedDeliveryError", "ComposedDeliveryReceipt", "ComposedFastDelivery"]
