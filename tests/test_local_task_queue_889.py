from __future__ import annotations

import pytest

from simplicio_loop.local_task_queue import LocalTaskQueue
from simplicio_loop.remote_queue import QueueConflict


def test_fencing_restart_dependencies_and_verified_completion(tmp_path):
    queue = LocalTaskQueue(tmp_path)
    queue.submit("a")
    queue.submit("b", depends_on=["a"])
    with pytest.raises(QueueConflict, match="dependencies"):
        queue.claim_local("b", "w", idempotency_key="b1")
    lease = queue.claim_local("a", "w", idempotency_key="a1", ttl=30)
    queue.persist_intent(lease, {"effect": "write"})
    queue.record_outcome(lease, "verified_success", receipt={"proof": "ok"})
    restarted = LocalTaskQueue(tmp_path)
    assert restarted.inspect_local("a")["outcome"]["outcome"] == "verified_success"
    second = restarted.claim_local("b", "w2", idempotency_key="b2")
    assert second.fencing_token == 1


def test_stop_prevents_claim_and_requests_active_cancellation(tmp_path):
    queue = LocalTaskQueue(tmp_path)
    queue.submit("a")
    queue.claim_local("a", "w", idempotency_key="a1")
    queue.submit("b")
    queue.stop()
    with pytest.raises(QueueConflict, match="stopped"):
        queue.claim_local("b", "w", idempotency_key="b1")
    assert queue.task("a")["lease"]["cancel_requested"] == 1
    queue.resume()
    assert queue.claim_local("b", "w", idempotency_key="b1").task_id == "b"


def test_unknown_outcome_requires_reconciliation_and_retry_provenance(tmp_path):
    queue = LocalTaskQueue(tmp_path)
    queue.submit("a")
    lease = queue.claim_local("a", "w", idempotency_key="a1")
    queue.record_outcome(lease, "unknown_outcome")
    with pytest.raises(QueueConflict, match="receipt"):
        queue.reconcile_unknown("a", verified=True)
    queue.reconcile_unknown("a", verified=False)
    retry = queue.claim_local("a", "w2", idempotency_key="a2")
    with pytest.raises(QueueConflict, match="provenance"):
        queue.record_outcome(retry, "retryable_failure")


def test_receipts_history_doctor_and_migration(tmp_path):
    queue = LocalTaskQueue(tmp_path)
    queue.submit("a")
    lease = queue.claim_local("a", "w", idempotency_key="a1")
    receipt = queue.record_outcome(
        lease, "retryable_failure", provenance={"idempotent": True}
    )
    assert receipt["digest"].startswith("sha256:")
    assert len(queue.inspect_local("a")["transitions"]) == 3
    assert queue.doctor_local()["healthy"] is True
    assert queue.migrate(dry_run=True)["dry_run"] is True
    migrated = queue.migrate(dry_run=False)
    assert migrated["dry_run"] is False
    assert queue.status_local()["journal_mode"] in {"wal", "delete"}
