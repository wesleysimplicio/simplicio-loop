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

from simplicio_loop.remote_queue import HTTPRemoteQueue, QueueUnavailable


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
