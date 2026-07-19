"""In-process tests for the #286 worker daemon: heartbeat loop + cancellation."""
from __future__ import annotations

import threading
import time

import pytest

from simplicio_loop.remote_queue import QueueConflict, SQLiteRemoteQueue
from simplicio_loop.worker_daemon import RemoteWorkerDaemon, TaskOutcome, sleep_in_slices


def _daemon(queue, agent_id, *, heartbeat_interval=0.05, lease_ttl=0.3):
    return RemoteWorkerDaemon(queue, agent_id=agent_id, heartbeat_interval=heartbeat_interval,
                              lease_ttl=lease_ttl)


def test_heartbeat_loop_keeps_lease_alive_past_original_ttl(tmp_path):
    """A single-shot lease's TTL is shorter than the total work time; only a live
    heartbeat loop (not the initial claim TTL) can keep it from expiring mid-work."""
    q = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    q.enqueue("T1")
    worker = _daemon(q, "agent-a", heartbeat_interval=0.05, lease_ttl=0.15)
    lease = worker.try_claim("T1", idempotency_key="k1")
    assert lease is not None

    def work(check_cancelled):
        # Longer than the 0.15s lease TTL: only survives if heartbeats renew it.
        time.sleep(0.4)
        return {"done": True}

    outcome = worker.run_task(lease, work, receipt_ref="receipts/T1.json")
    assert outcome.status == "completed"
    assert outcome.detail["result"]["done"] is True

    task = q.task("T1")
    assert task["status"] == "completed"


def test_second_claim_rejected_while_first_lease_active(tmp_path):
    q = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    q.enqueue("T1")
    worker_a = _daemon(q, "agent-a", lease_ttl=5.0)
    worker_b = _daemon(q, "agent-b", lease_ttl=5.0)
    lease_a = worker_a.try_claim("T1", idempotency_key="a-key")
    assert lease_a is not None
    lease_b = worker_b.try_claim("T1", idempotency_key="b-key")
    assert lease_b is None  # try_claim swallows QueueConflict and returns None


def test_cooperative_cancellation_aborts_work_and_releases_task(tmp_path):
    q = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    q.enqueue("T1")
    worker = _daemon(q, "agent-a", heartbeat_interval=0.05, lease_ttl=1.0)
    lease = worker.try_claim("T1", idempotency_key="k1")
    assert lease is not None

    cancel_after = threading.Event()

    def work(check_cancelled):
        # Signal the main thread that work has started, then poll for cancellation
        # cooperatively instead of racing a fixed sleep against the cancel call below.
        cancel_after.set()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if check_cancelled():
                return {"aborted": True}
            time.sleep(0.02)
        return {"aborted": False}

    def cancel_soon():
        cancel_after.wait(timeout=2.0)
        time.sleep(0.1)  # let at least one heartbeat land first
        worker.request_cancel("T1")

    canceller = threading.Thread(target=cancel_soon)
    canceller.start()
    outcome = worker.run_task(lease, work, receipt_ref="receipts/T1.json")
    canceller.join(timeout=2.0)

    assert outcome.status == "cancelled"
    task = q.task("T1")
    assert task["status"] == "ready"  # released back to the pool, not stuck "claimed"


class _FlakyAfterNHeartbeats:
    """Wraps a real queue but makes heartbeat #``fail_at`` onward raise, simulating the
    lease genuinely having been reclaimed elsewhere (or the queue becoming unreachable)
    without needing to win an actual timing race against a real second claimant."""

    def __init__(self, inner, *, fail_at: int) -> None:
        self._inner = inner
        self._fail_at = fail_at
        self._calls = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def heartbeat(self, lease, *, ttl=60.0):
        self._calls += 1
        if self._calls >= self._fail_at:
            raise QueueConflict("simulated: lease reclaimed by another worker")
        return self._inner.heartbeat(lease, ttl=ttl)


def test_lease_loss_mid_work_never_completes_or_releases_under_stale_fence(tmp_path):
    """Once this worker's heartbeat starts failing (queue reports the fence is stale --
    e.g. another worker genuinely reclaimed the task after a real TTL expiry), it must
    neither complete nor release the task using its now-stale fence: the outcome is
    reported as ``lease_lost``, distinct from a cooperative cancellation, and the task's
    queue-side state is left exactly as the (hypothetical) reclaimer would have set it."""
    q = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    q.enqueue("T1")
    real_lease = q.claim("T1", "agent-a", idempotency_key="a-key", ttl=5.0)
    flaky_queue = _FlakyAfterNHeartbeats(q, fail_at=2)
    worker_a = RemoteWorkerDaemon(flaky_queue, agent_id="agent-a", heartbeat_interval=0.05, lease_ttl=0.5)

    def work(check_cancelled):
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if check_cancelled():
                return {"aborted": True}
            time.sleep(0.02)
        return {"aborted": False}

    outcome = worker_a.run_task(real_lease, work, receipt_ref="receipts/T1.json")

    assert outcome.status == "lease_lost"
    # The underlying queue state is untouched by the losing worker: still claimed under
    # agent-a's original lease (a real reclaimer would have overwritten this, not this
    # worker) -- proving neither complete() nor release() fired on the stale fence.
    task = q.task("T1")
    assert task["status"] == "claimed"
    assert task["lease"]["agent_id"] == "agent-a"
    assert task["lease"]["fencing_token"] == real_lease.fencing_token


def test_sleep_in_slices_returns_false_when_cancelled_early():
    flag = {"value": False}
    assert sleep_in_slices(1.0, slice_seconds=0.05, check_cancelled=lambda: flag["value"]) is True

    flags = iter([False, False, True])

    def check():
        return next(flags, True)

    assert sleep_in_slices(1.0, slice_seconds=0.05, check_cancelled=check) is False


def test_request_cancel_on_task_without_active_lease_is_a_structured_conflict(tmp_path):
    q = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    q.enqueue("T1")
    worker = _daemon(q, "agent-a")
    with pytest.raises(QueueConflict):
        worker.request_cancel("T1")


def test_daemon_rejects_heartbeat_interval_not_smaller_than_ttl(tmp_path):
    q = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    with pytest.raises(ValueError):
        RemoteWorkerDaemon(q, agent_id="a", heartbeat_interval=1.0, lease_ttl=1.0)
