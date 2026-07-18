import tempfile
import time
from pathlib import Path

import pytest

from simplicio_loop.hub_queue_retry import HubRetryQueue, QueueLeaseError, QueueRetryError


def test_idempotent_submit_survives_restart_and_dead_letters_after_budget() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "queue.db")
        queue = HubRetryQueue(path)
        task_id = queue.submit({"kind": "test"}, idempotency_key="same", max_attempts=2)
        assert queue.submit({"kind": "changed"}, idempotency_key="same") == task_id

        first = queue.claim("worker-a", ttl=10)
        assert first is not None
        assert queue.fail(first, error_code="temporary") == "retry"
        second = queue.claim("worker-b", ttl=10)
        assert second is not None
        assert queue.fail(second, error_code="permanent") == "dead_letter"
        assert queue.dead_letters()[0]["error_code"] == "permanent"
        queue.close()

        restarted = HubRetryQueue(path)
        assert restarted.state(task_id) == "dead_letter"
        assert restarted.dead_letters()[0]["task_id"] == task_id
        restarted.requeue(task_id)
        assert restarted.state(task_id) == "queued"
        restarted.close()


def test_only_current_lease_can_heartbeat_or_complete() -> None:
    with tempfile.TemporaryDirectory() as directory:
        queue = HubRetryQueue(str(Path(directory) / "queue.db"))
        task_id = queue.submit({}, idempotency_key="key")
        lease = queue.claim("worker", ttl=10)
        assert lease is not None
        stale = type(lease)(task_id, "wrong", lease.fence, lease.expires_at)
        with pytest.raises(QueueLeaseError):
            queue.heartbeat(stale)
        queue.complete(lease)
        assert queue.state(task_id) == "completed"
        queue.close()


def test_expired_lease_is_reclaimed_by_another_worker() -> None:
    """A worker that crashes after claim (no heartbeat/fail/complete) must not
    strand the task: once the visibility timeout elapses, another worker can
    claim it, and the dead worker's lease is fenced off (#504 AC: "apenas um
    worker possui lease valida por tarefa" / lease renewal + expiration).
    """
    with tempfile.TemporaryDirectory() as directory:
        queue = HubRetryQueue(str(Path(directory) / "queue.db"))
        task_id = queue.submit({"kind": "test"}, idempotency_key="expiring")
        dead = queue.claim("worker-dead", ttl=0.05)
        assert dead is not None
        assert queue.claim("worker-live-too-soon") is None

        time.sleep(0.15)

        revived = queue.claim("worker-live", ttl=10)
        assert revived is not None
        assert revived.task_id == task_id
        assert revived.fence == dead.fence + 1
        assert queue.state(task_id) == "leased"

        # The crashed worker's old lease is fenced off, not the current owner.
        with pytest.raises(QueueLeaseError):
            queue.heartbeat(dead)
        with pytest.raises(QueueLeaseError):
            queue.complete(dead)

        # The new owner's lease is genuinely valid.
        queue.heartbeat(revived, ttl=10)
        queue.complete(revived)
        assert queue.state(task_id) == "completed"
        queue.close()


def test_invalid_requests_and_empty_queue_are_rejected() -> None:
    with tempfile.TemporaryDirectory() as directory:
        queue = HubRetryQueue(str(Path(directory) / "queue.db"))
        assert queue.claim("worker") is None
        with pytest.raises(QueueRetryError):
            queue.submit({}, idempotency_key="", max_attempts=0)
        queue.close()
