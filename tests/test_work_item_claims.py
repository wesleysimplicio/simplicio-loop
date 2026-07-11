import json
import time

import pytest

from simplicio_loop.remote_queue import QueueConflict, SQLiteRemoteQueue
from simplicio_loop.work_item_claims import AttemptCoordinator


IDENTITY = {
    "agent_id": "codex@device-a", "runtime": "codex", "device_id": "device-a",
    "session_id": "session-a", "capabilities": ["claim", "heartbeat", "fencing", "receipts", "events"],
}


def test_claim_scopes_context_and_fences_receipts(tmp_path):
    queue = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    coordinator = AttemptCoordinator(queue, run_id="run-1", receipt_dir=tmp_path / "receipts")
    attempt = coordinator.claim(work_item_id="WI-1", identity=IDENTITY, goal="ship one item",
                                acs=["AC-1"], source_refs=["src/a.py", "private.txt"],
                                allowed_paths=["src/a.py"])
    assert attempt.attempt_id == "WI-1-1"
    assert attempt.context["source_refs"] == ["src/a.py"]
    event = coordinator.record_event(attempt, "tool_called", {"tool": "pytest"})
    assert event["fencing_token"] == 1
    receipt = coordinator.accept_receipt(attempt, {"status": "passed"})
    assert receipt["work_item_id"] == "WI-1"
    assert receipt["attempt_id"] == "WI-1-1"
    assert receipt["agent"]["agent_id"] == IDENTITY["agent_id"]
    done = coordinator.complete(attempt, receipt_ref="receipts/WI-1/1.json")
    assert done["status"] == "completed"
    lines = (tmp_path / "receipts" / "run-1" / "WI-1" / "WI-1-1" / "events.jsonl").read_text().splitlines()
    assert [json.loads(line)["kind"] for line in lines] == ["claimed", "tool_called", "completed"]


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
