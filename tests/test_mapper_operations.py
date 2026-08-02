from __future__ import annotations

from dataclasses import dataclass

import pytest

from simplicio_loop.mapper_operations import (
    MapperOperationsAdapter,
    MapperOperationsError,
    OperationLease,
)
from simplicio_loop.remote_queue import QueueConflict


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

    def prepare_effect(self, *args, **kwargs):
        self._record("prepare_effect", *args, **kwargs)
        return {"status": "prepared"}

    def commit_effect(self, *args, **kwargs):
        self._record("commit_effect", *args, **kwargs)
        return {"status": "committed"}

    def mark_effect_unknown(self, *args, **kwargs):
        self._record("mark_effect_unknown", *args, **kwargs)
        return {"status": "unknown"}

    def reconcile_effect(self, *args, **kwargs):
        self._record("reconcile_effect", *args, **kwargs)
        return {"status": kwargs["outcome"]}

    def append_event(self, *args, **kwargs):
        self._record("append_event", *args, **kwargs)
        return {"status": "appended"}

    def replay(self, *args, **kwargs):
        self._record("replay", *args, **kwargs)
        return {"valid": True}

    def status(self, *args, **kwargs):
        self._record("status", *args, **kwargs)
        return {"counts": {}}


def test_adapter_translates_operation_lease_and_preserves_opaque_fence():
    fake = FakeStore([])
    adapter = MapperOperationsAdapter("/tmp/loop-ops.db", store=fake)
    lease = adapter.claim_next("worker-1", slot_id="slot-a", lease_seconds=9)
    assert lease == OperationLease(
        "task-1", "attempt-1", "fence-1", "lease-1", "worker-1", {"kind": "test"}
    )
    renewed = adapter.heartbeat(lease, lease_seconds=10)
    assert renewed.expires_at == 123.5
    assert fake.calls[0] == (
        "claim",
        ("worker-1",),
        {"slot_id": "slot-a", "lease_seconds": 9},
    )
    assert fake.calls[1][0] == "heartbeat"
    assert fake.calls[1][1] == ("attempt-1", "fence-1")


def test_adapter_passes_idempotency_and_receipt_without_reimplementing_storage():
    fake = FakeStore([])
    adapter = MapperOperationsAdapter("/tmp/loop-ops.db", store=fake)
    adapter.enqueue("task-1", {"x": 1}, idempotency_key="idem-1", priority=7)
    lease = adapter.claim_next("worker-1")
    adapter.complete(lease, receipt={"receipt_sha": "sha256:test"})
    enqueue_call = fake.calls[0]
    complete_call = fake.calls[2]
    assert enqueue_call == (
        "enqueue",
        ("task-1", {"x": 1}),
        {"idempotency_key": "idem-1", "priority": 7},
    )
    assert complete_call[1] == ("attempt-1", "fence-1")
    assert complete_call[2] == {
        "status": "completed",
        "receipt": {"receipt_sha": "sha256:test"},
    }


def test_adapter_exposes_non_queue_operations_and_no_local_state(tmp_path):
    fake = FakeStore([])
    database = tmp_path / "loop-ops.db"
    adapter = MapperOperationsAdapter(database, store=fake)
    lease = OperationLease("t", "a", "f", "l", "w", {})
    adapter.register_slot("slot", 2)
    adapter.release(lease)
    adapter.cancel("t")
    adapter.reclaim_expired()
    adapter.checkpoint(lease, 1, {"cursor": 1})
    adapter.prepare_effect(lease, effect_id="e", idempotency_key="i", payload={})
    adapter.commit_effect(lease, effect_id="e", receipt={"ok": True})
    adapter.mark_effect_unknown(lease, effect_id="e")
    adapter.reconcile_effect(
        effect_id="e",
        attempt_id="a",
        outcome="committed",
        receipt={"external": "proof"},
        fence_token=None,
    )
    adapter.append_event("run", "claimed", {}, expected_seq=0)
    assert adapter.replay("run")["valid"]
    assert adapter.status()["counts"] == {}
    assert not database.exists()


def test_unknown_effect_keeps_fence_and_reconciliation_is_explicit():
    fake = FakeStore([])
    adapter = MapperOperationsAdapter("/tmp/loop-ops.db", store=fake)
    lease = OperationLease("t", "attempt-1", "fence-1", "l", "w", {})
    adapter.mark_effect_unknown(lease, effect_id="effect-1")
    adapter.reconcile_effect(
        effect_id="effect-1",
        attempt_id=lease.attempt_id,
        outcome="failed",
        receipt={"reconciled": True},
    )
    assert fake.calls[0] == (
        "mark_effect_unknown",
        ("effect-1", "attempt-1", "fence-1"),
        {},
    )
    assert fake.calls[1] == (
        "reconcile_effect",
        ("effect-1", "attempt-1", None),
        {"outcome": "failed", "receipt": {"reconciled": True}},
    )


def test_adapter_maps_mapper_fencing_failure_to_queue_conflict(monkeypatch):
    class StoreError(Exception):
        reason_code = "STALE_FENCE"

    fake = FakeStore([])

    def fail(*args, **kwargs):
        raise StoreError("old fence")

    fake.heartbeat = fail
    adapter = MapperOperationsAdapter("/tmp/loop-ops.db", store=fake)
    lease = OperationLease("t", "a", "f", "l", "w", {})
    monkeypatch.setattr(
        "simplicio_loop.mapper_operations._load_store_error_type", lambda: StoreError
    )
    with pytest.raises(QueueConflict, match="STALE_FENCE"):
        adapter.heartbeat(lease)


def test_adapter_wraps_unexpected_mapper_failure():
    fake = FakeStore([])

    def fail(*args, **kwargs):
        raise ValueError("bad mapper response")

    fake.status = fail
    adapter = MapperOperationsAdapter("/tmp/loop-ops.db", store=fake)
    with pytest.raises(MapperOperationsError, match="MAPPER_OPERATION_FAILED"):
        adapter.status()
