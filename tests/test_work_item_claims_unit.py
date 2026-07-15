import json
import time

import pytest

from simplicio_loop.receipt_verifier import ReceiptSchema, ReceiptStatus
from simplicio_loop.remote_queue import QueueConflict, SQLiteRemoteQueue
from simplicio_loop.work_item_claims import AttemptCoordinator, ReceiptVerificationFailed


IDENTITY = {
    "agent_id": "codex@device-a", "runtime": "codex", "device_id": "device-a",
    "session_id": "session-a", "capabilities": ["claim", "heartbeat", "fencing", "receipts", "events"],
}


def test_claim_scopes_context_and_fences_receipts(tmp_path):
    queue = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    coordinator = AttemptCoordinator(queue, run_id="run-1", receipt_dir=tmp_path / "receipts")
    attempt = coordinator.claim(work_item_id="WI-1", identity=IDENTITY, goal="ship one item",
                                acs=["AC-1"], source_refs=["src/a.py", "private.txt"],
                                allowed_paths=["src/a.py"],
                                issue_ref="wesleysimplicio/simplicio-loop#183")
    assert attempt.attempt_id == "WI-1-1"
    assert attempt.context["source_refs"] == ["src/a.py"]
    assert attempt.context["issue_ref"] == "wesleysimplicio/simplicio-loop#183"
    event = coordinator.record_event(attempt, "tool_called", {"tool": "pytest"})
    assert event["fencing_token"] == 1
    assert event["agent"] == {key: IDENTITY[key] for key in ("agent_id", "runtime", "device_id", "session_id")}
    assert event["issue_ref"] == "wesleysimplicio/simplicio-loop#183"
    assert event["issue_url"] == "https://github.com/wesleysimplicio/simplicio-loop/issues/183"
    receipt = coordinator.accept_receipt(attempt, {"status": "passed"})
    assert receipt["work_item_id"] == "WI-1"
    assert receipt["attempt_id"] == "WI-1-1"
    assert receipt["agent"]["agent_id"] == IDENTITY["agent_id"]
    assert receipt["issue_ref"] == "wesleysimplicio/simplicio-loop#183"
    done = coordinator.complete(attempt, receipt_ref="receipts/WI-1/1.json")
    assert done["status"] == "completed"
    lines = (tmp_path / "receipts" / "run-1" / "WI-1" / "WI-1-1" / "events.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines]
    assert [record["kind"] for record in records] == ["claimed", "tool_called", "completed"]
    assert all(record["agent"] == event["agent"] for record in records)
    assert all(record["issue_ref"] == "wesleysimplicio/simplicio-loop#183" for record in records)


def test_lease_loss_rejects_receipt_and_retry_gets_new_attempt(tmp_path):
    queue = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    coordinator = AttemptCoordinator(queue, run_id="run-2")
    first = coordinator.claim(work_item_id="WI-2", identity=IDENTITY, goal="retry me", ttl=0.01)
    time.sleep(0.03)
    other = dict(IDENTITY, agent_id="claude@device-b", runtime="claude", device_id="device-b", session_id="session-b")
    second = AttemptCoordinator(queue, run_id="run-2").claim(work_item_id="WI-2", identity=other, goal="retry me")
    assert second.lease.fencing_token == first.lease.fencing_token + 1
    with pytest.raises(QueueConflict):
        coordinator.accept_receipt(first, {"status": "stale"})


def test_retry_releases_and_bumps_fence(tmp_path):
    queue = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    coordinator = AttemptCoordinator(queue, run_id="run-3")
    first = coordinator.claim(work_item_id="WI-3", identity=IDENTITY, goal="retry me", acs=["AC-1"])
    second = coordinator.retry(first, reason="validation_failed")
    assert second.attempt_id == "WI-3-2"
    assert second.lease.fencing_token == 2
    assert queue.events()[-1]["kind"] == "claimed"


# --- receipt_verifier wiring (issue #286: "receipt schema/hash verification" gap) -----

_TEST_SCHEMA = ReceiptSchema(
    name="test-receipt", required_fields=("status", "measured_at"),
    provenance_fields=("work_item_id", "attempt_id"),
)


def test_accept_receipt_without_schema_keeps_prior_existence_only_behavior(tmp_path):
    """Back-compat: no schema means no verdict is attached, same as before #286."""
    queue = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    coordinator = AttemptCoordinator(queue, run_id="run-4")
    attempt = coordinator.claim(work_item_id="WI-4", identity=IDENTITY, goal="ship")
    receipt = coordinator.accept_receipt(attempt, {"status": "passed"})
    assert "verification" not in receipt


def test_accept_receipt_with_schema_attaches_verified_verdict(tmp_path):
    queue = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    coordinator = AttemptCoordinator(queue, run_id="run-5")
    attempt = coordinator.claim(work_item_id="WI-5", identity=IDENTITY, goal="ship")
    receipt = coordinator.accept_receipt(
        attempt, {"status": "passed", "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        schema=_TEST_SCHEMA,
    )
    assert receipt["verification"]["status"] == ReceiptStatus.VERIFIED
    assert receipt["verification"]["verified"] is True


def test_accept_receipt_with_schema_rejects_missing_provenance(tmp_path):
    """A receipt missing schema-required content must never be silently recorded as if
    it were trustworthy -- this is exactly the #288 gap #286's remote-queue flow had not
    closed: any two files existing was previously enough."""
    queue = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    coordinator = AttemptCoordinator(queue, run_id="run-6")
    attempt = coordinator.claim(work_item_id="WI-6", identity=IDENTITY, goal="ship")
    with pytest.raises(ReceiptVerificationFailed) as excinfo:
        coordinator.accept_receipt(attempt, {"status": "passed"}, schema=_TEST_SCHEMA)
    assert excinfo.value.verdict.status == ReceiptStatus.MISSING_FIELD


def test_verify_and_complete_only_completes_the_lease_on_a_verified_receipt(tmp_path):
    queue = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    coordinator = AttemptCoordinator(queue, run_id="run-7")
    attempt = coordinator.claim(work_item_id="WI-7", identity=IDENTITY, goal="ship")
    result = coordinator.verify_and_complete(
        attempt, {"status": "passed", "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        receipt_ref="receipts/WI-7.json", schema=_TEST_SCHEMA,
    )
    assert result["status"] == "completed"
    assert result["verification"]["status"] == ReceiptStatus.VERIFIED
    assert queue.task("WI-7")["status"] == "completed"


def test_verify_and_complete_leaves_lease_active_when_receipt_fails_verification(tmp_path):
    """A failed verdict must not transition the queue's lease to completed -- the task
    stays claimed (mutable) so a corrected receipt or an explicit retry can follow,
    instead of the queue silently accepting an unverifiable result as done."""
    queue = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    coordinator = AttemptCoordinator(queue, run_id="run-8")
    attempt = coordinator.claim(work_item_id="WI-8", identity=IDENTITY, goal="ship")
    with pytest.raises(ReceiptVerificationFailed):
        coordinator.verify_and_complete(
            attempt, {"status": "passed"},  # missing measured_at -> MISSING_FIELD
            receipt_ref="receipts/WI-8.json", schema=_TEST_SCHEMA,
        )
    assert queue.task("WI-8")["status"] == "claimed"
    # The lease is still current -- a corrected receipt can still complete it.
    fixed = coordinator.verify_and_complete(
        attempt, {"status": "passed", "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        receipt_ref="receipts/WI-8.json", schema=_TEST_SCHEMA,
    )
    assert fixed["status"] == "completed"
