import subprocess
import sys
import threading
import time

import pytest

from simplicio_loop.remote_queue import HTTPRemoteQueue, QueueConflict, SQLiteRemoteQueue, create_http_queue_server


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


def test_http_adapter_preserves_atomic_claims_and_fencing(tmp_path):
    backend = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    backend.enqueue("T1", {"source": "github", "number": 185})
    server = create_http_queue_server(backend, token="secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = "http://127.0.0.1:%d" % server.server_port
        codex = HTTPRemoteQueue(url, token="secret")
        claude = HTTPRemoteQueue(url, token="secret")
        lease = codex.claim("T1", "codex@A", idempotency_key="run:T1", ttl=5,
                            identity={"agent_id": "codex@A", "runtime": "codex",
                                      "device_id": "laptop-a", "session_id": "s1",
                                      "capabilities": ["claim", "heartbeat", "fencing", "receipts"]})
        codex.assert_active(lease)
        assert codex.heartbeat(lease, ttl=5).fencing_token == 1
        with pytest.raises(QueueConflict):
            claude.claim("T1", "claude@B", idempotency_key="run:T1-other")
        codex.complete(lease, receipt_ref="receipts/T1.json")
        assert codex.events()[-1]["kind"] == "completed"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_unavailable_is_fail_closed():
    with pytest.raises(Exception) as error:
        HTTPRemoteQueue("http://127.0.0.1:1", timeout=0.05).events()
    assert "QueueUnavailable" in type(error.value).__name__


def test_network_bind_requires_explicit_tls(tmp_path):
    backend = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    with pytest.raises(ValueError, match="TLS is required"):
        create_http_queue_server(backend, host="0.0.0.0", token="secret")


def test_server_cli_requires_tls_pair(tmp_path):
    result = subprocess.run(
        [sys.executable, "scripts/remote_queue_server.py", "--db", str(tmp_path / "q.db"),
         "--token", "secret", "--tls-certfile", "only-cert.pem"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2
    assert "must be provided together" in (result.stderr + result.stdout)
