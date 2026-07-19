"""Live HTTP queue proof with process-isolated workers.

The unit tests for ``SQLiteRemoteQueue`` prove transaction semantics in one
process.  These tests exercise the actual HTTP transport and two independent
Python worker processes, which is the closest deterministic local proof of the
Codex/Claude multi-device contract without requiring external infrastructure.
TLS and production deployment are intentionally *not* inferred by this test.
"""

import multiprocessing
import threading
import time

import pytest

from simplicio_loop.remote_queue import (
    HTTPRemoteQueue,
    QueueConflict,
    SQLiteRemoteQueue,
    create_http_queue_server,
)


def _worker_claim(url, token, agent_id, barrier, result_queue):
    """Claim from a real HTTP endpoint in a separate process."""
    client = HTTPRemoteQueue(url, token=token, timeout=10)
    identity = {
        "agent_id": agent_id,
        "runtime": "codex" if agent_id.startswith("codex") else "claude",
        "device_id": agent_id.split("@", 1)[1],
        "session_id": "live-proof-" + agent_id,
        "capabilities": ["claim", "heartbeat", "fencing", "receipts"],
    }
    try:
        barrier.wait(timeout=10)
        lease = client.claim(
            "shared-task",
            agent_id,
            idempotency_key="live-proof:" + agent_id,
            ttl=5,
            identity=identity,
        )
        result_queue.put({"agent_id": agent_id, "status": "claimed", "token": lease.fencing_token})
    except QueueConflict as exc:
        result_queue.put({"agent_id": agent_id, "status": "conflict", "error": str(exc)})
    except Exception as exc:  # pragma: no cover - turns child failures into useful diagnostics
        result_queue.put({"agent_id": agent_id, "status": "error", "error": repr(exc)})


@pytest.fixture
def live_queue_server(tmp_path):
    backend = SQLiteRemoteQueue(str(tmp_path / "live-queue.db"))
    backend.enqueue("shared-task", {"source": "live-http-proof"})
    backend.enqueue("fenced-task")
    server = create_http_queue_server(backend, token="live-secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_port, backend
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_live_http_auth_two_process_workers_and_fencing(live_queue_server):
    """Auth, atomic claim, expiry reclaim, and stale-fence rejection over HTTP."""
    url, backend = live_queue_server

    with pytest.raises(ValueError, match="invalid queue token"):
        HTTPRemoteQueue(url, timeout=2).events()

    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    results = ctx.Queue()
    workers = [
        ctx.Process(target=_worker_claim, args=(url, "live-secret", "codex@machine-a", barrier, results)),
        ctx.Process(target=_worker_claim, args=(url, "live-secret", "claude@machine-b", barrier, results)),
    ]
    for worker in workers:
        worker.start()
    observed = [results.get(timeout=15) for _ in workers]
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0

    assert sorted(item["status"] for item in observed) == ["claimed", "conflict"]
    winner = next(item for item in observed if item["status"] == "claimed")
    assert winner["token"] == 1

    client_a = HTTPRemoteQueue(url, token="live-secret")
    stale = client_a.claim("fenced-task", "codex@machine-a", idempotency_key="fence:old", ttl=0.05)
    time.sleep(0.10)
    fresh = client_a.claim("fenced-task", "claude@machine-b", idempotency_key="fence:new", ttl=5)
    assert fresh.fencing_token == stale.fencing_token + 1
    with pytest.raises(QueueConflict, match="stale or expired"):
        client_a.complete(stale, receipt_ref="receipts/stale.json")
    completed = client_a.complete(fresh, receipt_ref="receipts/fresh.json")
    assert completed["fencing_token"] == fresh.fencing_token

    events = backend.events()
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert any(event["kind"] == "completed" and event["payload"]["receipt_ref"] == "receipts/fresh.json"
               for event in events)

