from __future__ import annotations

import pytest

from simplicio_loop.local_task_queue import LocalTaskQueue
from simplicio_loop.local_task_queue_cli import cli_main
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


def test_cancel_reclaim_drain_top_and_retention(tmp_path):
    queue = LocalTaskQueue(tmp_path)
    queue.submit("queued")
    assert [item["task_id"] for item in queue.top()] == ["queued"]
    assert queue.cancel_local("queued")["status"] == "cancelled"
    queue.submit("active")
    queue.claim_local("active", "w", idempotency_key="active", ttl=30)
    assert queue.cancel_local("active")["status"] == "cancelling"
    assert queue.drain(timeout=0)["status"] == "cancelling"
    queue.resume()
    queue.submit("stale")
    queue.claim_local("stale", "w", idempotency_key="stale", ttl=0.01)
    assert queue.reclaim_stale(now=10**30) == ["active", "stale"]
    queue.submit("done")
    lease = queue.claim_local("done", "w", idempotency_key="done")
    queue.record_outcome(lease, "verified_success", receipt={"proof": True})
    queue.release(lease)
    assert queue.gc_terminal()["eligible"] == ["done"]
    assert queue.gc_terminal(apply=True)["removed"] == ["done"]


def test_json_cli_surface(tmp_path, capsys):
    queue = LocalTaskQueue(tmp_path)
    queue.submit("a")
    repo = str(tmp_path)
    for args in (
        ["--repo", repo, "status"],
        ["--repo", repo, "top", "--limit", "1"],
        ["--repo", repo, "inspect", "a"],
        ["--repo", repo, "cancel", "a"],
        ["--repo", repo, "drain", "--timeout", "0"],
        ["--repo", repo, "resume"],
        ["--repo", repo, "doctor"],
        ["--repo", repo, "reclaim"],
        ["--repo", repo, "gc"],
    ):
        assert cli_main(args) == 0
        assert capsys.readouterr().out.strip().startswith(("{", "["))
