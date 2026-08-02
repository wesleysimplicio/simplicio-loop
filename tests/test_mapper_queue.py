from __future__ import annotations

from dataclasses import dataclass

import pytest

from simplicio_loop.mapper_operations import MapperOperationsAdapter, OperationLease
from simplicio_loop.mapper_queue import MAPPER_QUEUE_SCHEMA, MapperQueue


@dataclass
class FakeStore:
    calls: list[tuple[str, tuple, dict]]

    def _record(self, method, *args, **kwargs):
        self.calls.append((method, args, kwargs))

    def initialize(self):
        self._record("initialize")
        return {"status": "ready"}

    def capabilities(self):
        self._record("capabilities")
        return {"capabilities": {"fencing": True}}

    def register_slot(self, *args):
        self._record("register_slot", *args)
        return {"status": "registered"}

    def enqueue(self, *args, **kwargs):
        self._record("enqueue", *args, **kwargs)
        return {"status": "queued"}

    def claim(self, *args, **kwargs):
        self._record("claim", *args, **kwargs)
        return {
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "fence_token": "fence-1",
            "lease_id": "lease-1",
            "worker_id": "worker-1",
            "payload": {"kind": "test"},
        }

    def heartbeat(self, *args, **kwargs):
        self._record("heartbeat", *args, **kwargs)
        return {"expires_at": 123.5}

    def release(self, *args, **kwargs):
        self._record("release", *args, **kwargs)
        return {"status": "released"}

    def complete(self, *args, **kwargs):
        self._record("complete", *args, **kwargs)
        return {"status": kwargs["status"]}

    def cancel(self, *args, **kwargs):
        self._record("cancel", *args, **kwargs)
        return {"status": "cancelled"}

    def reclaim_expired(self):
        self._record("reclaim_expired")
        return {"recovered": 1}

    def checkpoint(self, *args, **kwargs):
        self._record("checkpoint", *args, **kwargs)
        return {"status": "checkpointed"}

    def status(self, *args, **kwargs):
        self._record("status", *args, **kwargs)
        return {"counts": {}}


def test_mapper_queue_delegates_lifecycle_without_local_database(tmp_path):
    fake = FakeStore([])
    database = tmp_path / "operations.sqlite"
    queue = MapperQueue(database, adapter=MapperOperationsAdapter(database, store=fake))
    assert queue.schema == MAPPER_QUEUE_SCHEMA
    assert queue.initialize() == {"status": "ready"}
    queue.submit("task-1", {"kind": "test"}, idempotency_key="idem-1", priority=3)
    lease = queue.claim_next("worker-1", slot_id="slot-a", lease_seconds=9)
    assert lease is not None
    renewed = queue.heartbeat(lease, lease_seconds=10)
    assert renewed.expires_at == 123.5
    queue.complete(renewed, receipt={"receipt_sha": "sha256:test"})
    queue.release(renewed)
    queue.reclaim_expired()
    queue.status()
    assert not database.exists()
    assert [call[0] for call in fake.calls] == [
        "initialize", "enqueue", "claim", "heartbeat", "complete", "release",
        "reclaim_expired", "status",
    ]


def test_mapper_queue_requires_idempotency_and_completion_receipt(tmp_path):
    fake = FakeStore([])
    queue = MapperQueue(tmp_path / "operations.sqlite", adapter=MapperOperationsAdapter(tmp_path / "operations.sqlite", store=fake))
    with pytest.raises(ValueError, match="idempotency_key"):
        queue.submit("task", {}, idempotency_key="")
    lease = OperationLease("task", "attempt", "fence", "lease", "worker", {})
    with pytest.raises(ValueError, match="receipt"):
        queue.complete(lease, receipt={})
