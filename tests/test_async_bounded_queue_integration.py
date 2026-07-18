"""Broader integration + concurrency stress for AsyncBoundedQueue (issue #508).

Scope note (read this before extending): a repo-wide search
(``grep -rl "AsyncBoundedQueue" --include='*.py'``) at the time this file was
written found **zero** production call sites outside `async_bounded_queue.py`
itself. PR #528/#535 shipped the queue and its benchmark in isolation; the
"Propagar pressão até ingestão e clientes" step from #508's plan, and the
ingestion/dispatch/reports wiring mentioned in #495's comments, have not
landed yet. There is therefore no real production pipeline to attach an
integration test to.

To still genuinely exercise "the queue as wired across stages" rather than
only the single-queue focused path, this file builds a real multi-stage
in-process pipeline (ingest -> dispatch -> report) out of three real
`AsyncBoundedQueue` instances driven by real asyncio tasks, and proves
backpressure genuinely propagates from the slow end of the pipeline back to
the producer. This is a constructed harness, not a test of existing
production wiring -- that wiring remains an open gap, called out explicitly
in the final report.
"""

from __future__ import annotations

import asyncio
from typing import List

from simplicio_loop.async_bounded_queue import AsyncBoundedQueue


async def _with_timeout(coro, seconds: float = 5.0):
    return await asyncio.wait_for(coro, seconds)


def test_three_stage_pipeline_propagates_backpressure_end_to_end() -> None:
    """ingest -> dispatch -> report -> sink, each hop a bounded queue. The
    final sink is throttled; prove the ingest producer genuinely blocks
    (real backpressure propagating three hops upstream), not just that the
    report queue itself rejects/waits at its own boundary.
    """

    async def scenario() -> None:
        ingest_q = AsyncBoundedQueue(2)
        dispatch_q = AsyncBoundedQueue(2)
        report_q = AsyncBoundedQueue(2)

        produced: List[int] = []
        dispatched: List[int] = []
        reported: List[int] = []
        sunk: List[int] = []
        total_items = 12

        async def ingest_producer() -> None:
            for i in range(total_items):
                await ingest_q.put(i)
                produced.append(i)
            await ingest_q.close()

        async def dispatch_stage() -> None:
            while True:
                try:
                    value, _, _ = await ingest_q.get()
                except Exception:
                    break
                ingest_q.task_done()
                dispatched.append(value)
                await dispatch_q.put(value)
            await dispatch_q.close()

        async def report_stage() -> None:
            while True:
                try:
                    value, _, _ = await dispatch_q.get()
                except Exception:
                    break
                dispatch_q.task_done()
                await report_q.put(value)
                reported.append(value)
            await report_q.close()

        async def report_sink() -> None:
            # Deliberately slow terminal consumer -- this is the throttle
            # that must propagate all the way back to ingest_producer.
            while True:
                try:
                    value, _, _ = await report_q.get()
                except Exception:
                    break
                await asyncio.sleep(0.02)
                report_q.task_done()
                sunk.append(value)

        producer_task = asyncio.create_task(ingest_producer())
        dispatch_task = asyncio.create_task(dispatch_stage())
        report_task = asyncio.create_task(report_stage())
        sink_task = asyncio.create_task(report_sink())

        # Snapshot early: the slow sink must have forced the upstream queues
        # to still be working, proving the producer could not race ahead
        # unbounded just because it is three hops away from the real
        # throttle.
        await asyncio.sleep(0.03)
        assert len(produced) < total_items, (
            "producer ran to completion instantly -- backpressure from the "
            "slow sink did not propagate upstream"
        )
        waited_somewhere = (
            ingest_q.status()["wait_count"]
            + dispatch_q.status()["wait_count"]
            + report_q.status()["wait_count"]
        )
        assert waited_somewhere >= 1

        await _with_timeout(producer_task, seconds=5.0)
        await _with_timeout(dispatch_task, seconds=5.0)
        await _with_timeout(report_task, seconds=5.0)
        await _with_timeout(sink_task, seconds=5.0)

        assert produced == list(range(total_items))
        assert dispatched == list(range(total_items))
        assert reported == list(range(total_items))
        assert sorted(sunk) == list(range(total_items))

        # No stage ever exceeded its own bound -- checked via each queue's
        # own accounting rather than trusting the pipeline blindly.
        assert ingest_q.status()["items"] == 0
        assert dispatch_q.status()["items"] == 0
        assert report_q.status()["items"] == 0

    asyncio.run(scenario())


def test_slow_downstream_bounds_upstream_queue_depth() -> None:
    """A tighter version of the same idea, focused purely on the numeric
    claim: at no point does the upstream queue exceed its configured bound,
    even though the producer is much faster than the consumer chain.
    """

    async def scenario() -> None:
        upstream = AsyncBoundedQueue(3)
        downstream = AsyncBoundedQueue(1)
        max_seen = 0

        async def fast_producer() -> None:
            for i in range(30):
                await upstream.put(i)

        async def relay_to_slow_downstream() -> None:
            nonlocal max_seen
            for _ in range(30):
                value, _, _ = await upstream.get()
                upstream.task_done()
                max_seen = max(max_seen, upstream.status()["items"])
                await asyncio.sleep(0.005)
                await downstream.put(value)

        async def slow_consumer() -> None:
            for _ in range(30):
                await downstream.get()

        producer_task = asyncio.create_task(fast_producer())
        relay_task = asyncio.create_task(relay_to_slow_downstream())
        consumer_task = asyncio.create_task(slow_consumer())

        await _with_timeout(asyncio.gather(producer_task, relay_task, consumer_task), seconds=5.0)

        assert max_seen <= upstream.max_items
        assert upstream.status()["rejected"] == 0  # overload="wait" never drops work

    asyncio.run(scenario())


def test_many_concurrent_producers_and_consumers_hold_bounds() -> None:
    """Real concurrency stress: many asyncio tasks producing and consuming
    against one shared bounded queue at once (the queue's actual concurrency
    model is single-event-loop + asyncio.Condition, so this is the faithful
    "real concurrency" for this primitive -- not multi-thread, since the
    class is not documented or designed to be thread-safe across loops).
    """

    async def scenario() -> None:
        producers_n = 20
        items_per_producer = 25
        consumers_n = 7
        total_items = producers_n * items_per_producer

        queue = AsyncBoundedQueue(5, max_bytes=15)
        produced_count = 0
        consumed_values: List[int] = []
        max_items_seen = 0
        max_bytes_seen = 0
        violations: List[str] = []

        def _check_bounds() -> None:
            nonlocal max_items_seen, max_bytes_seen
            status = queue.status()
            max_items_seen = max(max_items_seen, status["items"])
            max_bytes_seen = max(max_bytes_seen, status["bytes"])
            if status["items"] > queue.max_items:
                violations.append(f"items {status['items']} > {queue.max_items}")
            if queue.max_bytes and status["bytes"] > queue.max_bytes:
                violations.append(f"bytes {status['bytes']} > {queue.max_bytes}")

        async def producer(producer_id: int) -> None:
            nonlocal produced_count
            for i in range(items_per_producer):
                size = 1 + (i % 3)  # vary size 1..3 to stress max_bytes too
                await queue.put((producer_id, i), size=size)
                produced_count += 1
                _check_bounds()
                if i % 5 == 0:
                    await asyncio.sleep(0)  # yield to force interleaving

        async def consumer() -> None:
            while True:
                try:
                    value, _, _ = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    return
                queue.task_done()
                consumed_values.append(value)
                _check_bounds()

        producers = [asyncio.create_task(producer(pid)) for pid in range(producers_n)]
        consumers = [asyncio.create_task(consumer()) for _ in range(consumers_n)]

        await _with_timeout(asyncio.gather(*producers), seconds=10.0)
        await _with_timeout(queue.join(), seconds=10.0)
        for c in consumers:
            c.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)

        assert violations == []
        assert produced_count == total_items
        assert len(consumed_values) == total_items
        assert len(set(consumed_values)) == total_items  # every item delivered exactly once
        assert max_items_seen <= queue.max_items
        assert max_bytes_seen <= queue.max_bytes
        assert queue.status()["items"] == 0
        assert queue.status()["unfinished"] == 0

    asyncio.run(scenario())


def test_concurrent_independent_queues_isolated_across_real_os_threads() -> None:
    """AsyncBoundedQueue is asyncio.Condition-based -- it is not designed to
    be shared across OS threads/event loops. What genuinely must hold under
    real thread-level parallelism is *isolation*: N independent queue
    instances, each driven by its own event loop in its own OS thread, must
    not corrupt each other's state (no shared mutable class-level state).
    """
    import concurrent.futures

    def run_one_queue_in_its_own_loop(worker_id: int) -> dict:
        async def scenario() -> dict:
            queue = AsyncBoundedQueue(4, max_bytes=16)

            async def producer() -> None:
                for i in range(40):
                    await queue.put((worker_id, i), size=1)

            async def consumer() -> None:
                for _ in range(40):
                    await queue.get()
                    queue.task_done()

            await asyncio.gather(producer(), consumer())
            return queue.status()

        return asyncio.run(scenario())

    n_threads = 6
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_threads) as pool:
        results = list(pool.map(run_one_queue_in_its_own_loop, range(n_threads)))

    assert len(results) == n_threads
    for status in results:
        assert status["items"] == 0
        assert status["unfinished"] == 0
        assert status["accepted"] == 40
        assert status["max_items"] == 4  # each thread's own queue kept its own config
