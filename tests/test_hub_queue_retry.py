import subprocess
import sys
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


def test_invalid_requests_and_empty_queue_are_rejected() -> None:
    with tempfile.TemporaryDirectory() as directory:
        queue = HubRetryQueue(str(Path(directory) / "queue.db"))
        assert queue.claim("worker") is None
        with pytest.raises(QueueRetryError):
            queue.submit({}, idempotency_key="", max_attempts=0)
        queue.close()


def test_expired_lease_is_reclaimable_by_another_worker() -> None:
    with tempfile.TemporaryDirectory() as directory:
        queue = HubRetryQueue(str(Path(directory) / "queue.db"))
        task_id = queue.submit({}, idempotency_key="k")
        first = queue.claim("worker-a", ttl=0.05)
        assert first is not None
        time.sleep(0.1)

        second = queue.claim("worker-b", ttl=10)
        assert second is not None
        assert second.task_id == task_id
        assert second.lease_id != first.lease_id
        assert second.fence != first.fence

        with pytest.raises(QueueLeaseError):
            queue.heartbeat(first)
        with pytest.raises(QueueLeaseError):
            queue.complete(first)

        queue.complete(second)
        assert queue.state(task_id) == "completed"
        queue.close()


_CRASH_SCRIPT = """
import sys
from simplicio_loop.hub_queue_retry import HubRetryQueue

queue = HubRetryQueue(sys.argv[1])
queue.submit({"kind": "first"}, idempotency_key="first")
sys.stdout.write(str(len(open(sys.argv[1] + "-wal", "rb").read())))
sys.stdout.flush()
queue.submit({"kind": "second"}, idempotency_key="second")
import os
os._exit(0)
"""


def test_restart_after_crash_preserves_committed_writes_without_duplicates() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "queue.db")
        result = subprocess.run(
            [sys.executable, "-c", _CRASH_SCRIPT, path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        wal_size_after_first = int(result.stdout.strip())
        assert wal_size_after_first > 0

        restarted = HubRetryQueue(path)
        rows = restarted._db.execute(
            "SELECT idempotency_key,state FROM hub_jobs ORDER BY idempotency_key"
        ).fetchall()
        keys = [row["idempotency_key"] for row in rows]
        assert keys == ["first", "second"]
        assert len(set(row["idempotency_key"] for row in rows)) == len(rows)
        for row in rows:
            assert row["state"] == "queued"
        restarted.close()


def test_corrupt_wal_tail_fails_closed_and_keeps_last_valid_snapshot() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "queue.db")
        result = subprocess.run(
            [sys.executable, "-c", _CRASH_SCRIPT, path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        wal_size_after_first = int(result.stdout.strip())

        wal_path = Path(path + "-wal")
        assert wal_path.exists()
        full_size = wal_path.stat().st_size
        assert full_size > wal_size_after_first

        with open(wal_path, "r+b") as handle:
            handle.truncate(wal_size_after_first)

        restarted = HubRetryQueue(path)
        rows = restarted._db.execute(
            "SELECT idempotency_key FROM hub_jobs ORDER BY idempotency_key"
        ).fetchall()
        keys = [row["idempotency_key"] for row in rows]
        assert keys == ["first"]

        task_id = restarted.submit({"kind": "post-recovery"}, idempotency_key="third")
        assert restarted.state(task_id) == "queued"
        restarted.close()
