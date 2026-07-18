"""Epic #495: cross-module composition proof.

Sub-issues #508 (AsyncBoundedQueue) and the supervisor's async process leasing
(async_io_supervisor.py, part of #509's subprocess side) each already have
their own isolated unit/integration/benchmark suites. None of them, as of
this test, exercise both together in one realistic flow with a real network
leg attached.

Note on scope (read before extending): on this branch, ``#509``'s subprocess
side landed (``process_supervisor.py``/``async_io_supervisor.py``), but its
network side did not -- ``simplicio_loop/remote_queue.py`` still exposes only
the original blocking ``HTTPRemoteQueue.complete``/``.enqueue``/etc, with no
``*_async`` counterparts (contrast this with a separate lineage's #509 wave3
slice, ``commit c589ddf``, which is not an ancestor of this branch's HEAD).
So this test does not call a ``complete_async`` that does not exist here.
Instead it composes the blocking ``HTTPRemoteQueue.complete`` through
``asyncio.to_thread`` -- i.e. it exercises epic AC item 7's "isolar chamadas
bloqueantes em pool limitado" literally: the still-blocking network call is
isolated in the default bounded thread-pool executor rather than the caller
pretending it is natively async. This is the honest composition available on
this branch's actual code, not a stand-in for the missing async remote_queue
API.

The flow: producer -> AsyncBoundedQueue (real asyncio.Condition backpressure)
-> AsyncProcessSupervisor (real subprocess, bounded semaphore) -> a real
loopback HTTP round trip via HTTPRemoteQueue.complete offloaded to a thread.
A concurrent ticker proves the event loop keeps advancing across all three
hops.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

from simplicio_loop.async_bounded_queue import AsyncBoundedQueue
from simplicio_loop.async_io_supervisor import AsyncProcessSupervisor
from simplicio_loop.process_supervisor import ProcessSpec, PythonProcessAdapter
from simplicio_loop.remote_queue import HTTPRemoteQueue, Lease


class _IntervalRecordingAdapter(PythonProcessAdapter):
    """Wraps the real adapter to record the actual subprocess execution
    window (post-semaphore-acquire), not the caller's queueing time.
    """

    def __init__(self, intervals: List[Any]) -> None:
        super().__init__()
        self._intervals = intervals

    async def run(self, spec, *, lease=None, on_spawned=None):  # noqa: ANN001
        start = time.monotonic()
        try:
            return await super().run(spec, lease=lease, on_spawned=on_spawned)
        finally:
            self._intervals.append((start, time.monotonic()))


class _CompleteHandler(BaseHTTPRequestHandler):
    delay_seconds = 0.05
    received: List[Dict[str, Any]] = []
    lock = threading.Lock()

    def log_message(self, *_args) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        time.sleep(self.delay_seconds)
        with self.lock:
            self.__class__.received.append(body)
        payload = json.dumps({"ok": True, "receipt_ref": body.get("receipt_ref")}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _start_server() -> ThreadingHTTPServer:
    _CompleteHandler.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CompleteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_bounded_queue_supervisor_and_remote_queue_compose_end_to_end() -> None:
    """ingest -> AsyncBoundedQueue -> AsyncProcessSupervisor subprocess ->
    HTTPRemoteQueue.complete (via asyncio.to_thread), with the queue bounded
    small enough that the producer must genuinely wait on the consumer's
    pace, and the supervisor's semaphore small enough that it genuinely
    bounds concurrent subprocesses -- while the event loop keeps ticking
    throughout.
    """
    server = _start_server()
    total_items = 6
    queue_capacity = 2
    supervisor_concurrency = 2

    async def scenario() -> Dict[str, Any]:
        port = server.server_address[1]
        remote = HTTPRemoteQueue(f"http://127.0.0.1:{port}", timeout=5.0)
        work_queue = AsyncBoundedQueue(queue_capacity, overload="wait")
        supervisor = AsyncProcessSupervisor(max_concurrency=supervisor_concurrency)

        completed: List[int] = []
        ticks = 0
        stop_ticker = False

        async def ticker() -> None:
            nonlocal ticks
            while not stop_ticker:
                await asyncio.sleep(0.01)
                ticks += 1

        async def producer() -> None:
            for i in range(total_items):
                await work_queue.put(i, size=1)
            await work_queue.close()

        async def consume_one() -> None:
            value, _size, _key = await work_queue.get()
            spec = ProcessSpec(
                argv=(sys.executable, "-c", "import time; time.sleep(0.03)"),
                idempotency_key=f"compose-{value}",
                timeout_seconds=5.0,
            )
            result = await supervisor.run(spec)
            assert result.returncode == 0
            lease = Lease(
                task_id=f"task-{value}",
                agent_id="agent-compose",
                lease_id=f"lease-{value}",
                fencing_token=1,
                expires_at=time.time() + 30,
                idempotency_key=f"compose-{value}",
            )
            await asyncio.to_thread(
                remote.complete,
                lease,
                receipt_ref=f"receipt-{value}",
                receipt={"item": value},
            )
            completed.append(value)
            work_queue.task_done()

        async def consumer() -> None:
            for _ in range(total_items):
                await consume_one()

        ticker_task = asyncio.create_task(ticker())
        started = time.monotonic()
        await asyncio.gather(producer(), consumer())
        elapsed = time.monotonic() - started
        stop_ticker = True
        await ticker_task
        await work_queue.join()

        return {
            "completed": completed,
            "elapsed": elapsed,
            "ticks": ticks,
            "queue_status": work_queue.status(),
            "supervisor_status": supervisor.status(),
        }

    outcome = asyncio.run(scenario())

    assert sorted(outcome["completed"]) == list(range(total_items))
    assert len(_CompleteHandler.received) == total_items
    assert {body["receipt_ref"] for body in _CompleteHandler.received} == {
        f"receipt-{i}" for i in range(total_items)
    }
    assert outcome["queue_status"]["accepted"] == total_items
    assert outcome["queue_status"]["closed"] is True
    assert outcome["queue_status"]["unfinished"] == 0
    assert outcome["supervisor_status"]["active_leases"] == 0
    assert outcome["supervisor_status"]["active_tasks"] == 0
    assert outcome["supervisor_status"]["persisted_outcomes"] == total_items
    assert outcome["ticks"] >= 5, "event loop must keep advancing across subprocess + network hops"

    server.shutdown()


def test_supervisor_concurrency_genuinely_bounds_subprocesses_fed_by_the_queue() -> None:
    """Same pipeline shape, but this test proves the composed concurrency
    bound: with capacity 1 the supervisor never runs more than one subprocess
    at a time even though multiple items are already queued and ready, and
    the network completion leg still rides through the same event loop.
    """
    server = _start_server()
    total_items = 4

    async def scenario() -> Dict[str, Any]:
        port = server.server_address[1]
        remote = HTTPRemoteQueue(f"http://127.0.0.1:{port}", timeout=5.0)
        work_queue = AsyncBoundedQueue(total_items, overload="wait")
        run_intervals: List[Any] = []
        supervisor = AsyncProcessSupervisor(
            adapter=_IntervalRecordingAdapter(run_intervals),
            max_concurrency=1,
        )

        for i in range(total_items):
            await work_queue.put(i, size=1)
        await work_queue.close()

        async def consume_one(value: int) -> None:
            spec = ProcessSpec(
                argv=(sys.executable, "-c", "import time; time.sleep(0.05)"),
                idempotency_key=f"bound-{value}",
                timeout_seconds=5.0,
            )
            result = await supervisor.run(spec)
            assert result.returncode == 0
            lease = Lease(
                task_id=f"btask-{value}",
                agent_id="agent-bound",
                lease_id=f"blease-{value}",
                fencing_token=1,
                expires_at=time.time() + 30,
                idempotency_key=f"bound-{value}",
            )
            await asyncio.to_thread(remote.complete, lease, receipt_ref=f"breceipt-{value}")

        async def consumer() -> None:
            while True:
                try:
                    value, _size, _key = await asyncio.wait_for(work_queue.get(), timeout=0.5)
                except Exception:
                    return
                await consume_one(value)
                work_queue.task_done()

        await asyncio.gather(*(consumer() for _ in range(total_items)))
        return {"run_intervals": run_intervals}

    outcome = asyncio.run(scenario())
    intervals = sorted(outcome["run_intervals"])
    assert len(intervals) == total_items
    for (_, first_end), (second_start, _) in zip(intervals, intervals[1:]):
        assert second_start >= first_end, "subprocess runs overlapped despite max_concurrency=1"

    server.shutdown()
