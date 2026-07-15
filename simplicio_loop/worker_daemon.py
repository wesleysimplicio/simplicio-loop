"""A real worker daemon: heartbeat loop + cooperative cancellation (issue #286).

The remainder of this epic already gave a worker the primitives it needs (a durable
lease with a fencing token, a discovery ``pull()`` endpoint, and an
``AttemptCoordinator``/``run_guarded`` heartbeat during a *subprocess*). What was still
missing was a standalone worker *process* that:

  1. discovers and claims one task on its own (no local coordinator driving it),
  2. keeps its lease alive with a background heartbeat for the life of an
     arbitrarily long, non-subprocess unit of work,
  3. cooperatively aborts the moment either (a) the queue reports the lease has been
     cancelled via ``RemoteQueue.request_cancel`` or (b) the lease was lost (reclaimed
     by someone else, or the queue is unreachable) -- distinguishing the two so a
     caller can tell "I was told to stop" from "I am no longer allowed to continue",
  4. releases the task back to ``ready`` on cancellation/lease-loss instead of leaving
     it stuck ``claimed`` forever, and completes it (with a receipt) on success.

This module is transport agnostic: it only depends on the ``RemoteQueue`` protocol in
``remote_queue.py``, so the same class drives both the in-process ``SQLiteRemoteQueue``
and the networked ``HTTPRemoteQueue`` -- which is exactly what lets one real, unmocked
two-process end-to-end test (a genuine worker daemon in each OS process, talking to a
shared SQLite file) exercise it.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .remote_queue import Lease, QueueConflict, QueueUnavailable, RemoteQueue

# A unit of work the daemon runs while it heartbeats. ``check_cancelled`` returns True the
# moment the caller must stop; a cooperative work function is expected to poll it and return
# early rather than run to completion regardless of the flag.
WorkFn = Callable[[Callable[[], bool]], Mapping[str, Any]]


class WorkerCancelled(RuntimeError):
    """Raised by :meth:`RemoteWorkerDaemon.run_task` when the queue asked to cancel.

    Distinguished from :class:`WorkerLeaseLost` so a caller can tell an operator-issued
    cancellation apart from an involuntary loss of exclusivity.
    """


class WorkerLeaseLost(RuntimeError):
    """Raised when a heartbeat during work discovers the lease is no longer current.

    This covers both "someone else reclaimed the task after this lease's TTL expired"
    and "the queue became unreachable" -- either way the worker must stop mutating
    immediately rather than keep working under a false assumption of exclusivity.
    """


@dataclass(frozen=True)
class TaskOutcome:
    """The terminal result of one claim-heartbeat-work-complete cycle."""

    task_id: str
    status: str  # "completed" | "cancelled" | "lease_lost"
    detail: Dict[str, Any]


class RemoteWorkerDaemon:
    """Claims work from a :class:`RemoteQueue`, heartbeats it, and honors cancellation.

    Unlike ``work_item_claims.AttemptCoordinator.run_guarded`` (which heartbeats around a
    *subprocess* with a fixed timeout), this drives an arbitrary Python callable
    (``WorkFn``) for as long as it likes, and treats "the queue told me to stop" as a
    first-class, non-exceptional outcome distinct from a hard failure.
    """

    def __init__(self, queue: RemoteQueue, *, agent_id: str, capabilities: Sequence[str] = (),
                 heartbeat_interval: float = 1.0, lease_ttl: float = 5.0) -> None:
        if not str(agent_id or "").strip():
            raise ValueError("agent_id is required")
        if heartbeat_interval <= 0 or lease_ttl <= 0:
            raise ValueError("heartbeat_interval and lease_ttl must be positive")
        if heartbeat_interval >= lease_ttl:
            raise ValueError("heartbeat_interval must be smaller than lease_ttl "
                              "so at least one heartbeat lands before the lease expires")
        self.queue = queue
        self.agent_id = str(agent_id).strip()
        self.capabilities = tuple(capabilities)
        self.heartbeat_interval = float(heartbeat_interval)
        self.lease_ttl = float(lease_ttl)

    def discover(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        """Return ready, capability-eligible task summaries for this worker."""
        return self.queue.pull(self.agent_id, capabilities=self.capabilities, limit=limit)

    def try_claim(self, task_id: str, *, idempotency_key: str) -> Optional[Lease]:
        """Claim one task; return ``None`` (never raise) if another worker already won it."""
        try:
            return self.queue.claim(task_id, self.agent_id, idempotency_key=idempotency_key,
                                    ttl=self.lease_ttl, capabilities=self.capabilities)
        except QueueConflict:
            return None

    def run_task(self, lease: Lease, work_fn: WorkFn, *, receipt_ref: str) -> TaskOutcome:
        """Run ``work_fn`` while heartbeating ``lease`` in a background thread.

        The background thread heartbeats every ``heartbeat_interval`` seconds. Each
        heartbeat both (a) extends the lease's TTL, keeping it alive, and (b) reports
        whether the queue has recorded a cancellation for the current fencing token. The
        moment either a cancellation is observed or the heartbeat itself fails (lease
        lost / queue unreachable), ``check_cancelled()`` starts returning ``True`` so a
        cooperative ``work_fn`` can stop, and this method itself never calls
        ``queue.complete`` for a lease it no longer believes is current.
        """
        stop_work = threading.Event()
        heartbeat_done = threading.Event()
        state: Dict[str, Any] = {"lease": lease, "cancelled": False, "lease_lost": None}
        lock = threading.Lock()

        def check_cancelled() -> bool:
            with lock:
                return state["cancelled"] or state["lease_lost"] is not None

        def _heartbeat_loop() -> None:
            try:
                while not stop_work.wait(self.heartbeat_interval):
                    try:
                        current = self.queue.heartbeat(state["lease"], ttl=self.lease_ttl)
                    except (QueueConflict, QueueUnavailable) as exc:
                        with lock:
                            state["lease_lost"] = exc
                        return
                    with lock:
                        state["lease"] = current
                        if current.cancelled:
                            state["cancelled"] = True
                            return
            finally:
                heartbeat_done.set()

        heartbeat_thread = threading.Thread(target=_heartbeat_loop, name="worker-heartbeat", daemon=True)
        heartbeat_thread.start()
        try:
            result = work_fn(check_cancelled)
        finally:
            stop_work.set()
            heartbeat_thread.join(timeout=self.heartbeat_interval * 4 + 1)

        with lock:
            current_lease = state["lease"]
            lease_lost = state["lease_lost"]
            cancelled = state["cancelled"]

        if lease_lost is not None:
            # A lease we no longer hold must never be released or completed -- both
            # mutate queue state under a fencing token that is (by definition here)
            # already stale, and would either silently steal the reclaimer's lease
            # back or produce a confusing duplicate "released" event.
            return TaskOutcome(lease.task_id, "lease_lost", {"error": str(lease_lost)})

        if cancelled or check_cancelled():
            release_result = self.queue.release(current_lease, reason="cancelled")
            return TaskOutcome(lease.task_id, "cancelled", release_result)

        completed = self.queue.complete(current_lease, receipt_ref=receipt_ref)
        return TaskOutcome(lease.task_id, "completed", {**completed, "result": dict(result)})

    def request_cancel(self, task_id: str, *, reason: str = "cancelled") -> Dict[str, Any]:
        return self.queue.request_cancel(task_id, reason=reason)


def sleep_in_slices(total_seconds: float, *, slice_seconds: float,
                    check_cancelled: Callable[[], bool]) -> bool:
    """Cooperative sleep helper: sleeps in small slices, polling ``check_cancelled``.

    Returns ``True`` if the full duration elapsed, ``False`` if it returned early
    because ``check_cancelled()`` became true. Shared by the CLI daemon and tests so a
    simulated unit of "work" is actually interruptible rather than a single blocking
    ``time.sleep`` that ignores cancellation until it returns.
    """
    if slice_seconds <= 0:
        raise ValueError("slice_seconds must be positive")
    remaining = float(total_seconds)
    while remaining > 0:
        if check_cancelled():
            return False
        step = min(slice_seconds, remaining)
        time.sleep(step)
        remaining -= step
    return not check_cancelled()


__all__ = [
    "RemoteWorkerDaemon", "TaskOutcome", "WorkFn", "WorkerCancelled", "WorkerLeaseLost",
    "sleep_in_slices",
]
