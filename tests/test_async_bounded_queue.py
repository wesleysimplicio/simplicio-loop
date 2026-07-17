from __future__ import annotations

import asyncio

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
