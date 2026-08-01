from __future__ import annotations

import pytest

from simplicio_loop.composed_fast_delivery import ComposedDeliveryError, ComposedFastDelivery
from simplicio_loop.fast_task_bridge import FastTaskBinding


def binding(task: str, worktree: str, base: str = "base") -> FastTaskBinding:
    return FastTaskBinding(task, "attempt", worktree, "mapper", "context", base,
                           f"overlay-{worktree}", f"lease-{task}", "2.0.20", f"receipt-{task}")


class Queue:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    def enqueue_merge(self, task_id: str):
        self.enqueued.append(task_id)
        return {"status": "queued", "task_id": task_id}

    def run_composed_verification(self, task_id, commands, suite="composed"):
        return {"passed": True, "task_id": task_id, "suite": suite}


def test_candidates_share_base_and_enter_composed_verification() -> None:
    queue = Queue()
    delivery = ComposedFastDelivery(queue)
    delivery.register_binding(binding("a", "w-a"))
    delivery.register_binding(binding("b", "w-b"))
    receipt = delivery.enqueue_verified_candidate("a", commands=[["python", "-c", "pass"]], candidate_order=1)
    assert queue.enqueued == ["a"]
    assert receipt.verification_status == "accepted"
    assert receipt.base_generation == "base"


def test_drift_and_overlay_reuse_fail_closed() -> None:
    delivery = ComposedFastDelivery(Queue())
    delivery.register_binding(binding("a", "w-a"))
    with pytest.raises(ComposedDeliveryError, match="base generation"):
        delivery.register_binding(binding("b", "w-b", base="base-2"))
    with pytest.raises(ComposedDeliveryError, match="worktree overlay"):
        delivery.register_binding(binding("c", "w-a"))
