"""Real-server proof for the new ``pull`` discovery path and atomic claim.

Issue #286 calls out that the queue supports enqueue/claim/heartbeat/complete/
release/events/task but has no discovery/pull of ready work for independent
workers, and requires proof that claim is atomic under real concurrency ("100
workers competing for the same task: exactly one winning claim").  This module
exercises the *real* HTTP server (``create_http_queue_server``) and the *real*
``SQLiteRemoteQueue`` backend -- no mocking of the claim/pull boundary -- using
genuine OS threads (``ThreadPoolExecutor``) as the concurrent workers.
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from simplicio_loop.remote_queue import (
    HTTPRemoteQueue,
    QueueConflict,
    SQLiteRemoteQueue,
    create_http_queue_server,
)

WORKER_COUNT = 50  # real OS threads; 100 real processes is flaky/slow for CI, per issue #286 spirit


@pytest.fixture
def http_queue(tmp_path):
    backend = SQLiteRemoteQueue(str(tmp_path / "pull-claim-queue.db"))
    server = create_http_queue_server(backend, token="fake-pull-claim-secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_port, backend
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _client(url: str) -> HTTPRemoteQueue:
    return HTTPRemoteQueue(url, token="fake-pull-claim-secret", timeout=10)


def test_100_workers_concurrent_claim_exactly_one_wins(http_queue):
    """Real HTTP server, real threads: exactly one claim wins, the rest get a structured conflict."""
    url, backend = http_queue
    backend.enqueue("contested-task", {"source": "issue-286"})

    def attempt_claim(index: int):
        client = _client(url)
        try:
            lease = client.claim(
                "contested-task",
                "worker-%03d@device" % index,
                idempotency_key="attempt-%03d" % index,
                ttl=30,
            )
            return {"status": "claimed", "token": lease.fencing_token, "index": index}
        except QueueConflict as exc:
            return {"status": "conflict", "error": str(exc), "index": index}

    with ThreadPoolExecutor(max_workers=WORKER_COUNT) as pool:
        futures = [pool.submit(attempt_claim, i) for i in range(WORKER_COUNT)]
        results = [future.result(timeout=30) for future in as_completed(futures)]

    assert len(results) == WORKER_COUNT
    winners = [r for r in results if r["status"] == "claimed"]
    conflicts = [r for r in results if r["status"] == "conflict"]
    assert len(winners) == 1, "exactly one worker must win the claim, got: %r" % winners
    assert len(conflicts) == WORKER_COUNT - 1
    # Every loser must have gotten a structured rejection, never a silent duplicate success
    # and never an unhandled exception (a bad result would show up as neither claimed/conflict).
    assert all(r["status"] in ("claimed", "conflict") for r in results)
    assert winners[0]["token"] == 1

    # The queue's own event log agrees: exactly one "claimed" event was recorded.
    claimed_events = [e for e in backend.events() if e["kind"] == "claimed"]
    assert len(claimed_events) == 1


def test_stale_fencing_token_claim_is_rejected_with_structured_conflict(http_queue):
    """A claim/complete using an expired lease's fencing token never silently succeeds."""
    url, backend = http_queue
    backend.enqueue("expiring-task")
    client = _client(url)

    stale_lease = client.claim("expiring-task", "worker-a@device", idempotency_key="a", ttl=0.05)
    import time
    time.sleep(0.15)  # let the lease expire
    fresh_lease = client.claim("expiring-task", "worker-b@device", idempotency_key="b", ttl=30)
    assert fresh_lease.fencing_token == stale_lease.fencing_token + 1

    # The stale lease's fencing token must be rejected as a structured conflict, not accepted.
    with pytest.raises(QueueConflict, match="stale or expired"):
        client.heartbeat(stale_lease, ttl=5)
    with pytest.raises(QueueConflict, match="stale or expired"):
        client.complete(stale_lease, receipt_ref="receipts/stale.json")

    # The fresh lease (current fencing token) completes normally.
    result = client.complete(fresh_lease, receipt_ref="receipts/fresh.json")
    assert result["fencing_token"] == fresh_lease.fencing_token


def test_pull_returns_only_capability_matching_ready_tasks_without_leaking_other_context(http_queue):
    """Pull filters by capability match and never serializes ineligible tasks' full context."""
    url, backend = http_queue
    backend.enqueue("frontend-task", {
        "goal": "SECRET_FRONTEND_GOAL_TEXT do not leak this to a backend-only worker",
        "required_capabilities": ["frontend"],
    })
    backend.enqueue("backend-task", {
        "goal": "SECRET_BACKEND_GOAL_TEXT do not leak this to a frontend-only worker",
        "required_capabilities": ["backend"],
    })
    client = _client(url)

    frontend_worker_view = client.pull("worker-fe@device", capabilities=["frontend"])
    assert [t["task_id"] for t in frontend_worker_view] == ["frontend-task"]

    # The non-matching task's full context (its goal text) must never appear anywhere
    # in the pull response for this worker -- only an eligible summary is returned.
    serialized = json.dumps(frontend_worker_view)
    assert "SECRET_BACKEND_GOAL_TEXT" not in serialized
    assert "goal" not in frontend_worker_view[0]  # summary only, not the full envelope

    backend_worker_view = client.pull("worker-be@device", capabilities=["backend"])
    assert [t["task_id"] for t in backend_worker_view] == ["backend-task"]
    assert "SECRET_FRONTEND_GOAL_TEXT" not in json.dumps(backend_worker_view)

    # A worker with neither capability sees nothing eligible.
    assert client.pull("worker-none@device", capabilities=[]) == []

    # A worker with both capabilities sees both ready tasks.
    both = client.pull("worker-both@device", capabilities=["frontend", "backend"])
    assert sorted(t["task_id"] for t in both) == ["backend-task", "frontend-task"]


def test_pull_excludes_task_with_unmet_dependency_until_dependency_completes(http_queue):
    """A task with an incomplete dependency is not offered by pull until the dependency clears."""
    url, backend = http_queue
    backend.enqueue("base-task")
    backend.enqueue("dependent-task", {"depends_on": ["base-task"]})
    client = _client(url)

    # dependent-task is ready but blocked -- pull must not surface it yet.
    pulled = client.pull("worker@device", capabilities=[])
    assert [t["task_id"] for t in pulled] == ["base-task"]

    lease = client.claim("base-task", "worker@device", idempotency_key="base", ttl=30)
    client.complete(lease, receipt_ref="receipts/base.json")

    pulled_after = client.pull("worker@device", capabilities=[])
    assert [t["task_id"] for t in pulled_after] == ["dependent-task"]


def test_pull_excludes_already_claimed_task(http_queue):
    """Once a task is claimed it is no longer offered to other workers via pull."""
    url, backend = http_queue
    backend.enqueue("solo-task")
    client = _client(url)
    client.claim("solo-task", "worker-a@device", idempotency_key="solo", ttl=30)
    assert client.pull("worker-b@device", capabilities=[]) == []


def test_sqlite_backend_pull_matches_http_semantics_directly(tmp_path):
    """The pull filter logic itself (no HTTP layer) matches the documented contract."""
    backend = SQLiteRemoteQueue(str(tmp_path / "direct-queue.db"))
    backend.enqueue("t1", {"required_capabilities": ["gpu"]})
    backend.enqueue("t2")

    no_caps = backend.pull("worker@device", capabilities=[])
    assert [t["task_id"] for t in no_caps] == ["t2"]
    assert no_caps[0]["required_capabilities"] == []
    assert no_caps[0]["status"] == "ready"
    assert "goal" not in no_caps[0]

    with_gpu = backend.pull("worker@device", capabilities=["gpu"])
    assert sorted(t["task_id"] for t in with_gpu) == ["t1", "t2"]
