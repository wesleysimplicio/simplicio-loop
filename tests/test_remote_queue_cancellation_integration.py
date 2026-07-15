"""Cooperative cancellation proof for issue #286 (SQLite + real HTTP backend).

``request_cancel`` records a durable cancellation flag against the *current* lease's
fencing token; the claimant discovers it the next time it heartbeats (or calls
``assert_active`` indirectly through the worker daemon), never via a side channel that
could race the fencing/expiry rules already proven in ``test_remote_queue.py`` and
``test_remote_queue_pull_claim.py``.
"""
from __future__ import annotations

import threading

import pytest

from simplicio_loop.remote_queue import (
    HTTPRemoteQueue,
    QueueConflict,
    SQLiteRemoteQueue,
    create_http_queue_server,
)


def test_request_cancel_flags_the_active_lease_and_is_seen_on_next_heartbeat(tmp_path):
    q = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    q.enqueue("T1")
    lease = q.claim("T1", "agent-a", idempotency_key="k1", ttl=30)
    assert lease.cancelled is False

    result = q.request_cancel("T1", reason="operator stop")
    assert result["cancel_requested"] is True
    assert result["fencing_token"] == lease.fencing_token

    renewed = q.heartbeat(lease, ttl=30)
    assert renewed.cancelled is True


def test_request_cancel_without_active_lease_is_a_structured_conflict(tmp_path):
    q = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    q.enqueue("T1")  # never claimed
    with pytest.raises(QueueConflict):
        q.request_cancel("T1")


def test_cancel_flag_does_not_survive_reclaim_by_a_new_lease(tmp_path):
    """A cancellation is scoped to the fencing token that existed when it was issued;
    once that lease genuinely expires and a new one is claimed, the new lease starts
    uncancelled -- cancellation is not a property of the task forever."""
    q = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    q.enqueue("T1")
    old = q.claim("T1", "agent-a", idempotency_key="a", ttl=0.2)
    q.request_cancel("T1")
    import time
    time.sleep(0.3)
    new = q.claim("T1", "agent-b", idempotency_key="b", ttl=30)
    assert new.cancelled is False
    assert q.heartbeat(new, ttl=30).cancelled is False


def test_cancel_over_real_http_server_round_trips_the_flag(tmp_path):
    server = create_http_queue_server(SQLiteRemoteQueue(str(tmp_path / "queue.db")),
                                      host="127.0.0.1", port=0, token="secret-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = "http://127.0.0.1:%d" % server.server_port
        client = HTTPRemoteQueue(base_url, token="secret-token")
        client.enqueue("T1")
        lease = client.claim("T1", "agent-a", idempotency_key="k1", ttl=30)
        assert lease.cancelled is False

        cancel_result = client.request_cancel("T1", reason="operator stop")
        assert cancel_result["cancel_requested"] is True

        renewed = client.heartbeat(lease, ttl=30)
        assert renewed.cancelled is True
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_cancel_over_http_without_active_lease_returns_409(tmp_path):
    server = create_http_queue_server(SQLiteRemoteQueue(str(tmp_path / "queue.db")),
                                      host="127.0.0.1", port=0, token="secret-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = "http://127.0.0.1:%d" % server.server_port
        client = HTTPRemoteQueue(base_url, token="secret-token")
        client.enqueue("T1")
        with pytest.raises(QueueConflict):
            client.request_cancel("T1")
    finally:
        server.shutdown()
        thread.join(timeout=5)
