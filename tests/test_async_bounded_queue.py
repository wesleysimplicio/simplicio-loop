from __future__ import annotations

import asyncio
import time

import pytest

from simplicio_loop.async_bounded_queue import (
    AsyncBoundedQueue,
    BackpressureError,
    QueueClosed,
)


def test_capacity_and_reject_backpressure() -> None:
    async def scenario() -> None:
        queue = AsyncBoundedQueue(1, overload="reject")
        await queue.put("one", size=3)
        with pytest.raises(BackpressureError) as error:
            await queue.put("two", size=1)
        assert error.value.receipt["reason"] == "full"
        assert queue.status()["rejected"] == 1

    asyncio.run(scenario())


def test_producer_waits_for_consumer_event() -> None:
    async def scenario() -> None:
        queue = AsyncBoundedQueue(1)
        await queue.put("one")
        producer = asyncio.create_task(queue.put("two"))
        await asyncio.sleep(0)
        assert not producer.done()
        assert queue.status()["wait_count"] == 1
        value, _, _ = await queue.get()
        queue.task_done()
        assert value == "one"
        assert (await producer)["accepted"] is True
        value, _, _ = await queue.get()
        queue.task_done()
        assert value == "two"

    asyncio.run(scenario())


def test_byte_budget_and_timeout_are_bounded() -> None:
    async def scenario() -> None:
        queue = AsyncBoundedQueue(4, max_bytes=4)
        await queue.put("one", size=4)
        with pytest.raises(BackpressureError) as error:
            await queue.put("two", size=1, timeout=0.01)
        assert error.value.receipt["reason"] == "timeout"
        assert error.value.receipt["queued_bytes"] == 4

    asyncio.run(scenario())


def test_coalescing_replaces_without_growing_queue() -> None:
    async def scenario() -> None:
        queue = AsyncBoundedQueue(2, max_bytes=10, coalesce=True)
        await queue.put({"v": 1}, size=4, key="same")
        result = await queue.put({"v": 2}, size=2, key="same")
        assert result["coalesced"] is True
        assert queue.status()["items"] == 1
        value, size, key = await queue.get()
        assert value == {"v": 2} and size == 2 and key == "same"
        queue.task_done()
        await queue.join()

    asyncio.run(scenario())


def test_join_and_close_wake_idle_consumer() -> None:
    async def scenario() -> None:
        queue = AsyncBoundedQueue(1)
        consumer = asyncio.create_task(queue.get())
        await asyncio.sleep(0)
        await queue.close()
        with pytest.raises(QueueClosed):
            await consumer
        assert queue.status()["closed"] is True

    asyncio.run(scenario())


def test_cancelled_put_does_not_leak_capacity() -> None:
    async def scenario() -> None:
        queue = AsyncBoundedQueue(1)
        await queue.put("one")
        waiter = asyncio.create_task(queue.put("two"))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        value, _, _ = await queue.get()
        queue.task_done()
        assert value == "one"
        await queue.put("two")
        value, _, _ = await queue.get()
        queue.task_done()
        assert value == "two"

    asyncio.run(scenario())


def test_no_background_worker_or_unbounded_task() -> None:
    async def scenario() -> None:
        queue = AsyncBoundedQueue(2)
        before = len(asyncio.all_tasks())
        await queue.put("x")
        after = len(asyncio.all_tasks())
        assert after == before
        assert queue.status()["max_items"] == 2

    asyncio.run(scenario())


def test_idle_consumer_burns_near_zero_cpu() -> None:
    async def scenario() -> None:
        queue = AsyncBoundedQueue(1)
        consumer = asyncio.create_task(queue.get())
        await asyncio.sleep(0)
        wall_started = time.perf_counter()
        cpu_started = time.process_time()
        await asyncio.sleep(0.2)
        cpu_elapsed = time.process_time() - cpu_started
        wall_elapsed = time.perf_counter() - wall_started
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert wall_elapsed > 0.15
        assert cpu_elapsed < 0.05

    asyncio.run(scenario())


def test_concurrent_producers_never_exceed_bound() -> None:
    async def scenario() -> None:
        queue = AsyncBoundedQueue(3, overload="wait")
        max_len = 0
        stop = False

        async def sampler() -> None:
            nonlocal max_len
            while not stop:
                max_len = max(max_len, queue.status()["items"])
                await asyncio.sleep(0)

        async def producer(count: int) -> None:
            for i in range(count):
                await queue.put(i)

        async def consumer(count: int) -> None:
            for _ in range(count):
                await asyncio.sleep(0)
                await queue.get()
                queue.task_done()

        sampler_task = asyncio.create_task(sampler())
        per_producer = 10
        producer_count = 4
        producers = [
            asyncio.create_task(producer(per_producer)) for _ in range(producer_count)
        ]
        await consumer(per_producer * producer_count)
        await asyncio.gather(*producers)
        stop = True
        await sampler_task

        assert max_len <= 3
        assert queue.status()["accepted"] == per_producer * producer_count

    asyncio.run(scenario())


def test_concurrent_producers_reject_exact_bound() -> None:
    async def scenario() -> None:
        queue = AsyncBoundedQueue(2, overload="reject")
        results = await asyncio.gather(
            *(queue.put(i) for i in range(5)), return_exceptions=True
        )
        accepted = [r for r in results if not isinstance(r, Exception)]
        rejected = [r for r in results if isinstance(r, BackpressureError)]
        assert len(accepted) == 2
        assert len(rejected) == 3
        assert queue.status()["items"] == 2
        assert queue.status()["rejected"] == 3

    asyncio.run(scenario())


def test_constructor_rejects_invalid_limits_and_overload() -> None:
    with pytest.raises(ValueError):
        AsyncBoundedQueue(0)
    with pytest.raises(ValueError):
        AsyncBoundedQueue(1, max_bytes=-1)
    with pytest.raises(ValueError):
        AsyncBoundedQueue(1, overload="drop")


def test_put_rejects_oversized_item_before_enqueue() -> None:
    async def scenario() -> None:
        queue = AsyncBoundedQueue(4, max_bytes=4)
        with pytest.raises(ValueError):
            await queue.put("too-big", size=5)
        with pytest.raises(ValueError):
            await queue.put("negative", size=-1)
        assert queue.status()["items"] == 0

    asyncio.run(scenario())


def test_put_on_closed_queue_raises_immediately() -> None:
    async def scenario() -> None:
        queue = AsyncBoundedQueue(2)
        await queue.close()
        with pytest.raises(QueueClosed):
            await queue.put("x")

    asyncio.run(scenario())


def test_closing_wakes_waiting_producer_with_queue_closed() -> None:
    async def scenario() -> None:
        queue = AsyncBoundedQueue(1, overload="wait")
        await queue.put("one")
        waiter = asyncio.create_task(queue.put("two"))
        await asyncio.sleep(0)
        assert not waiter.done()
        await queue.close()
        with pytest.raises(QueueClosed):
            await waiter

    asyncio.run(scenario())


def test_put_with_already_expired_timeout_raises_without_waiting() -> None:
    async def scenario() -> None:
        queue = AsyncBoundedQueue(1)
        await queue.put("one")
        with pytest.raises(BackpressureError) as error:
            await queue.put("two", timeout=0)
        assert error.value.receipt["reason"] == "timeout"

    asyncio.run(scenario())


def test_task_done_called_too_many_times_raises() -> None:
    queue = AsyncBoundedQueue(1)
    with pytest.raises(ValueError):
        queue.task_done()


def test_join_waits_for_pending_work_then_returns() -> None:
    async def scenario() -> None:
        queue = AsyncBoundedQueue(2)
        await queue.put("one")
        joiner = asyncio.create_task(queue.join())
        await asyncio.sleep(0)
        assert not joiner.done()
        _, _, _ = await queue.get()
        queue.task_done()
        await joiner

    asyncio.run(scenario())


def test_reopen_rejects_pending_work_then_succeeds_once_drained() -> None:
    async def scenario() -> None:
        queue = AsyncBoundedQueue(1)
        await queue.put("one")
        await queue.close()
        with pytest.raises(RuntimeError):
            await queue.reopen()
        await queue.get()
        queue.task_done()
        await queue.reopen()
        assert queue.status()["closed"] is False
        await queue.put("two")
        value, _, _ = await queue.get()
        queue.task_done()
        assert value == "two"

    asyncio.run(scenario())


def test_coalescing_does_not_starve_distinct_key() -> None:
    async def scenario() -> None:
        queue = AsyncBoundedQueue(5, coalesce=True)
        await queue.put("a1", key="hot")
        await queue.put("b1", key="cold")
        await queue.put("a2", key="hot")
        await queue.put("a3", key="hot")

        order = []
        for _ in range(2):
            value, _, key = await queue.get()
            queue.task_done()
            order.append((key, value))

        assert order[0] == ("hot", "a3")
        assert order[1] == ("cold", "b1")
        assert queue.status()["coalesced"] == 2

    asyncio.run(scenario())
