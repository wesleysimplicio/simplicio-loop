from __future__ import annotations

from dataclasses import dataclass

import pytest

from simplicio_loop.mapper_operations import MapperOperationsAdapter
from simplicio_loop.mapper_remote_queue import (
    MAPPER_QUEUE_SCHEMA,
    MapperRemoteQueue,
)


@dataclass
class FakeOperationsStore:
    calls: list[tuple[str, tuple, dict]]

    def _record(self, method, *args, **kwargs):
        self.calls.append((method, args, kwargs))

    def initialize(self):
        self._record("initialize")
        return {"status": "ready"}

    def list_ready(self, *, limit):
        self._record("list_ready", limit=limit)
        return {
            "tasks": [
                {
                    "task_id": "task-1",
                    "payload": {"kind": "test", "required_capabilities": ["python"]},
                    "updated_at": "now",
                }
            ]
        }

    def enqueue(self, *args, **kwargs):
        self._record("enqueue", *args, **kwargs)
        return {"status": "queued"}

    def claim_task(self, *args, **kwargs):
        self._record("claim_task", *args, **kwargs)
        return {
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "fence_token": "fence-1",
            "lease_id": "lease-1",
            "worker_id": "worker-1",
            "payload": {"kind": "test"},
            "expires_at": 10.0,
        }

    def heartbeat(self, *args, **kwargs):
        self._record("heartbeat", *args, **kwargs)
        return {"expires_at": 20.0, "cancelled": False}

    def assert_active(self, *args, **kwargs):
        self._record("assert_active", *args, **kwargs)
        return {"status": "active"}

    def complete(self, *args, **kwargs):
        self._record("complete", *args, **kwargs)
        return {"status": "completed"}

    def request_cancel(self, *args, **kwargs):
        self._record("request_cancel", *args, **kwargs)
        return {"cancel_requested": True}

    def release(self, *args, **kwargs):
        self._record("release", *args, **kwargs)
        return {"status": "released"}

    def status(self, *args, **kwargs):
        self._record("status", *args, **kwargs)
        return {"state": "completed"}


def test_mapper_remote_queue_delegates_without_creating_local_sqlite(tmp_path):
    fake = FakeOperationsStore([])
    database = tmp_path / "operations.sqlite"
    queue = MapperRemoteQueue(
        database,
        adapter=MapperOperationsAdapter(database, store=fake, auto_create=False),
    )
    assert queue.schema == MAPPER_QUEUE_SCHEMA
    assert queue.initialize() == {"status": "ready"}
    queue.enqueue("task-1", {"kind": "test"})
    assert queue.pull("worker-1", capabilities=("python",)) == [
        {
            "task_id": "task-1",
            "status": "ready",
            "required_capabilities": ["python"],
            "depends_on": [],
            "updated_at": "now",
        }
    ]
    lease = queue.claim("task-1", "worker-1", idempotency_key="claim-1")
    queue.assert_active(lease)
    lease = queue.heartbeat(lease)
    receipt = queue.build_completion_receipt(
        task_id=lease.task_id,
        agent_id=lease.agent_id,
        fencing_token=lease.fencing_token,
        receipt_ref="receipts/task-1.json",
    )
    assert queue.complete(lease, receipt_ref="receipts/task-1.json", receipt=receipt)["status"] == "completed"
    assert queue.request_cancel("task-1")["cancel_requested"] is True
    queue.release(lease)
    assert queue.task("task-1")["state"] == "completed"
    assert not database.exists()
    assert [call[0] for call in fake.calls] == [
        "initialize", "enqueue", "list_ready", "claim_task", "assert_active",
        "heartbeat", "complete", "request_cancel", "release", "status",
    ]


def test_mapper_remote_queue_rejects_missing_or_mismatched_receipt(tmp_path):
    fake = FakeOperationsStore([])
    database = tmp_path / "operations.sqlite"
    queue = MapperRemoteQueue(
        database,
        adapter=MapperOperationsAdapter(database, store=fake, auto_create=False),
    )
    lease = queue.claim("task-1", "worker-1", idempotency_key="claim-1")
    with pytest.raises(Exception, match="RECEIPT_REQUIRED"):
        queue.complete(lease, receipt_ref="receipt.json", receipt=None)
