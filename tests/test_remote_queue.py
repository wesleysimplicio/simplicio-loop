import threading
import time

import pytest

from simplicio_loop.remote_queue import QueueConflict, SQLiteRemoteQueue


def test_idempotent_claim_and_ordered_reconnect_events(tmp_path):
    q = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    q.enqueue("T1", {"goal": "docs"})
    a = q.claim("T1", "codex@machine-a", idempotency_key="run:T1", ttl=5)
    assert q.claim("T1", "codex@machine-a", idempotency_key="run:T1") == a
    assert q.heartbeat(a, ttl=5).fencing_token == 1
    q.complete(a, receipt_ref="receipts/T1.json")
    events = q.events()
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert events[-1]["kind"] == "completed"


def test_expiry_reclaim_increments_fence_and_rejects_stale_worker(tmp_path):
    q = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    q.enqueue("T1")
    old = q.claim("T1", "codex@A", idempotency_key="a", ttl=0.01)
    time.sleep(0.03)
    new = q.claim("T1", "claude@B", idempotency_key="b", ttl=5)
    assert new.fencing_token == old.fencing_token + 1
    with pytest.raises(QueueConflict):
        q.complete(old, receipt_ref="stale")
    q.complete(new, receipt_ref="fresh")


def test_idempotency_key_cannot_be_reused_for_another_task(tmp_path):
    q = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    q.enqueue("T1")
    q.enqueue("T2")
    q.claim("T1", "codex@A", idempotency_key="same-key")
    with pytest.raises(QueueConflict):
        q.claim("T2", "codex@A", idempotency_key="same-key")


def test_two_agents_only_one_atomic_claim_wins(tmp_path):
    path = str(tmp_path / "queue.db")
    q = SQLiteRemoteQueue(path)
    q.enqueue("T1")
    results = []
    barrier = threading.Barrier(2)

    def worker(agent):
        local = SQLiteRemoteQueue(path)
        barrier.wait()
        try:
            results.append(local.claim("T1", agent, idempotency_key=agent, ttl=5).agent_id)
        except QueueConflict:
            results.append("conflict")

    threads = [threading.Thread(target=worker, args=("codex@A",)), threading.Thread(target=worker, args=("claude@B",))]
    for t in threads: t.start()
    for t in threads: t.join()
    assert results.count("conflict") == 1
    assert sum(value != "conflict" for value in results) == 1
