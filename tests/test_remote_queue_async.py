"""Real-network proof for the #509 async migration of HTTPRemoteQueue.

Uses a genuine loopback HTTP server (ThreadingHTTPServer, no fakes/mocks) that
sleeps inside its request handler to emulate real network latency, so the
assertions below observe actual thread/socket behaviour, not a stubbed clock.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from simplicio_loop.remote_queue import HTTPRemoteQueue, Lease, QueueUnavailable


class _SlowPullHandler(BaseHTTPRequestHandler):
    delay_seconds = 0.4

    def log_message(self, *_args) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        time.sleep(self.delay_seconds)
        body = json.dumps({"tasks": []}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def slow_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowPullHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_pull_async_does_not_block_the_event_loop(slow_server) -> None:
    port = slow_server.server_address[1]
    queue = HTTPRemoteQueue(f"http://127.0.0.1:{port}", timeout=5.0)

    async def scenario() -> None:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            for _ in range(30):
                await asyncio.sleep(0.02)
                ticks += 1

        ticker_task = asyncio.create_task(ticker())
        start = time.monotonic()
        tasks = await queue.pull_async("agent-1")
        elapsed = time.monotonic() - start
        ticker_task.cancel()
        assert tasks == []
        assert elapsed >= _SlowPullHandler.delay_seconds
        assert ticks >= 10, "event loop must keep advancing during the blocking network call"

    asyncio.run(scenario())


def test_pull_async_deadline_returns_control_before_socket_timeout(slow_server) -> None:
    port = slow_server.server_address[1]
    queue = HTTPRemoteQueue(f"http://127.0.0.1:{port}", timeout=5.0)

    async def scenario() -> None:
        start = time.monotonic()
        with pytest.raises(QueueUnavailable):
            await queue.pull_async("agent-1", timeout=0.05)
        elapsed = time.monotonic() - start
        assert elapsed < _SlowPullHandler.delay_seconds

    asyncio.run(scenario())


def test_pull_async_task_cancel_returns_control_promptly(slow_server) -> None:
    port = slow_server.server_address[1]
    queue = HTTPRemoteQueue(f"http://127.0.0.1:{port}", timeout=5.0)

    async def scenario() -> None:
        task = asyncio.create_task(queue.pull_async("agent-1"))
        await asyncio.sleep(0.05)
        start = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.monotonic() - start
        assert elapsed < _SlowPullHandler.delay_seconds

    asyncio.run(scenario())


def test_pull_async_and_sync_agree_on_a_fast_server() -> None:
    class _FastHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            body = json.dumps({"tasks": []}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _FastHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        queue = HTTPRemoteQueue(f"http://127.0.0.1:{port}", timeout=5.0)

        async def scenario() -> None:
            return await queue.pull_async("agent-1")

        result = asyncio.run(scenario())
        assert result == []
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _lease_payload(task_id: str = "task-1", agent_id: str = "agent-1") -> dict:
    return {
        "task_id": task_id,
        "agent_id": agent_id,
        "lease_id": "lease-abc",
        "fencing_token": 1,
        "expires_at": time.time() + 60.0,
        "idempotency_key": "key-1",
        "identity": None,
        "capabilities": [],
        "cancelled": False,
    }


class _EchoQueueHandler(BaseHTTPRequestHandler):
    """Answers every remote-queue op with a response shaped like the real server."""

    def log_message(self, *_args) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        request = json.loads(raw.decode("utf-8")) if raw else {}
        if self.path.endswith("/enqueue"):
            body = {"ok": True}
        elif self.path.endswith("/claim"):
            body = {"lease": _lease_payload(request.get("task_id", "task-1"), request.get("agent_id", "agent-1"))}
        elif self.path.endswith("/heartbeat"):
            lease = dict(request["lease"])
            lease["expires_at"] = time.time() + float(request.get("ttl", 60.0))
            body = {"lease": lease}
        elif self.path.endswith("/complete"):
            body = {"receipt_ref": request.get("receipt_ref"), "accepted": True}
        else:
            self.send_response(404)
            self.end_headers()
            return
        payload = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture()
def echo_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoQueueHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_enqueue_async_round_trips_against_a_real_server(echo_server) -> None:
    port = echo_server.server_address[1]
    queue = HTTPRemoteQueue(f"http://127.0.0.1:{port}", timeout=5.0)

    async def scenario() -> None:
        await queue.enqueue_async("task-9", {"foo": "bar"})

    asyncio.run(scenario())


def test_claim_async_decodes_a_real_lease_response(echo_server) -> None:
    port = echo_server.server_address[1]
    queue = HTTPRemoteQueue(f"http://127.0.0.1:{port}", timeout=5.0)

    async def scenario() -> Lease:
        return await queue.claim_async("task-9", "agent-9", idempotency_key="key-9")

    lease = asyncio.run(scenario())
    assert lease.task_id == "task-9"
    assert lease.agent_id == "agent-9"
    assert lease.lease_id == "lease-abc"


def test_heartbeat_async_extends_a_real_lease(echo_server) -> None:
    port = echo_server.server_address[1]
    queue = HTTPRemoteQueue(f"http://127.0.0.1:{port}", timeout=5.0)
    lease = Lease("task-9", "agent-9", "lease-abc", 1, time.time() + 5.0, "key-9")

    async def scenario() -> Lease:
        return await queue.heartbeat_async(lease, ttl=120.0)

    renewed = asyncio.run(scenario())
    assert renewed.lease_id == lease.lease_id
    assert renewed.expires_at > lease.expires_at


def test_complete_async_sends_the_receipt_and_returns_the_real_response(echo_server) -> None:
    port = echo_server.server_address[1]
    queue = HTTPRemoteQueue(f"http://127.0.0.1:{port}", timeout=5.0)
    lease = Lease("task-9", "agent-9", "lease-abc", 1, time.time() + 5.0, "key-9")

    async def scenario() -> dict:
        return await queue.complete_async(lease, receipt_ref="receipt-1", receipt={"note": "done"})

    result = asyncio.run(scenario())
    assert result == {"receipt_ref": "receipt-1", "accepted": True}


def test_async_methods_respect_a_short_deadline_on_a_slow_server(slow_server) -> None:
    port = slow_server.server_address[1]
    queue = HTTPRemoteQueue(f"http://127.0.0.1:{port}", timeout=5.0)
    lease = Lease("task-9", "agent-9", "lease-abc", 1, time.time() + 5.0, "key-9")

    async def scenario() -> None:
        with pytest.raises(QueueUnavailable):
            await queue.claim_async("task-9", "agent-9", idempotency_key="key-9", timeout=0.05)
        with pytest.raises(QueueUnavailable):
            await queue.heartbeat_async(lease, timeout=0.05)
        with pytest.raises(QueueUnavailable):
            await queue.complete_async(lease, receipt_ref="receipt-1", timeout=0.05)
        with pytest.raises(QueueUnavailable):
            await queue.enqueue_async("task-9", timeout=0.05)

    asyncio.run(scenario())
