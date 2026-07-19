"""Restart/crash-recovery stress tests for AsyncBoundedQueue (issue #508).

PR #528 and PR #535 covered capacity, backpressure, timeout/cancellation and
benchmarking of the *focused* queue path. PR #535's own body flagged the gap
this file closes: "integration/restart stress beyond the focused queue path
remains tracked in #508".

These tests read the queue's real contract from `async_bounded_queue.py`
rather than inventing stronger guarantees:

- `get()` pops the item from the internal deque *before* the consumer does
  any work with it. There is no built-in redelivery: if the consumer task is
  killed/cancelled after `get()` returns but before `task_done()` runs, that
  specific item is gone for good (at-most-once, matching stdlib
  `asyncio.Queue`/`queue.Queue` semantics) — capacity (items/bytes) is freed
  immediately regardless, but the `unfinished` counter used by `join()` stays
  incremented until something accounts for it.
- `reopen()` refuses to reopen while `_items` or `_unfinished` is non-zero,
  which is the mechanism that prevents a "restart" from silently discarding
  unresolved work.

Every test is timeout-guarded (`asyncio.wait_for`) so a real deadlock fails
the test instead of hanging the suite.
"""

from __future__ import annotations

import asyncio

import pytest

from simplicio_loop.async_bounded_queue import AsyncBoundedQueue, QueueClosed


async def _with_timeout(coro, seconds: float = 2.0):
    return await asyncio.wait_for(coro, seconds)


def test_consumer_crash_mid_flight_frees_capacity_without_deadlock() -> None:
    """Killing a consumer after get() but before task_done() must not wedge
    the queue: capacity is freed immediately (no unbounded growth), later
    puts/gets keep working (no deadlock), and only join() -- which depends on
    task_done() bookkeeping -- reflects the outstanding debt.
    """

    async def scenario() -> None:
        queue = AsyncBoundedQueue(1)
        await queue.put("work-item")

        async def crashing_consumer() -> str:
            value, _, _ = await queue.get()
            await asyncio.sleep(10)  # simulates work that never completes
            queue.task_done()
            return value

        task = asyncio.create_task(crashing_consumer())
        await asyncio.sleep(0.01)  # let it reach get() and pop the item
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        status = queue.status()
        assert status["items"] == 0
        assert status["bytes"] == 0
        assert status["unfinished"] == 1  # crashed item's debt is still owed

        # Capacity was freed: a fresh put must not block/deadlock.
        await _with_timeout(queue.put("next-item"))
        assert queue.status()["items"] == 1

        # join() must NOT silently lie: the crashed item's debt was never
        # paid, so join() correctly refuses to return until it is.
        with pytest.raises(asyncio.TimeoutError):
            await _with_timeout(queue.join(), seconds=0.2)

        # Recovery: a supervisor accounts for the crashed item explicitly,
        # then drains the rest normally.
        queue.task_done()  # pays down the crashed item's debt
        value, _, _ = await queue.get()
        assert value == "next-item"
        queue.task_done()
        await _with_timeout(queue.join())

    asyncio.run(scenario())


def test_reopen_refuses_to_discard_unresolved_work_then_succeeds_after_drain() -> None:
    """reopen() is the queue's own guard against a "restart" that would
    silently drop pending work. Prove both halves: it raises while debt is
    outstanding, and it genuinely works once the debt is paid.
    """

    async def scenario() -> None:
        queue = AsyncBoundedQueue(2)
        await queue.put("a")
        await queue.put("b")
        await queue.close()

        with pytest.raises(RuntimeError):
            await queue.reopen()

        value, _, _ = await queue.get()
        queue.task_done()
        assert value == "a"

        with pytest.raises(RuntimeError):
            await queue.reopen()  # "b" is still queued and unfinished

        value, _, _ = await queue.get()
        queue.task_done()
        assert value == "b"

        await queue.reopen()
        assert queue.status()["closed"] is False

        # Queue is genuinely usable again post-restart.
        await _with_timeout(queue.put("post-restart"))
        value, _, _ = await _with_timeout(queue.get())
        queue.task_done()
        assert value == "post-restart"

    asyncio.run(scenario())


def test_join_waiter_wakes_when_concurrent_task_done_reaches_zero() -> None:
    """join() blocks in condition.wait() while unfinished > 0. The wake path
    (task_done -> call_soon_threadsafe -> a fire-and-forget notify task) is
    unexercised by the existing focused suite because task_done() there
    always runs sequentially *before* join() is awaited. Force the race: a
    consumer task pays down the last debt *while* join() is already parked,
    proving the cross-task wake genuinely fires instead of relying on a
    same-coroutine notify that would mask a real bug.
    """

    async def scenario() -> None:
        queue = AsyncBoundedQueue(2)
        await queue.put("only-item")

        async def delayed_consumer() -> None:
            await asyncio.sleep(0.05)
            _, _, _ = await queue.get()
            queue.task_done()

        consumer_task = asyncio.create_task(delayed_consumer())
        await _with_timeout(queue.join(), seconds=2.0)
        await consumer_task
        assert queue.status()["unfinished"] == 0

    asyncio.run(scenario())


def test_repeated_close_reopen_restart_cycles_do_not_leak() -> None:
    """A long-running process restarts the queue many times (e.g. a
    supervised worker bouncing). Each cycle must fully release state: no
    growth in queued items/bytes/unfinished across cycles, and no leaked
    asyncio tasks from the notify-on-drain machinery.
    """

    async def scenario() -> None:
        queue = AsyncBoundedQueue(3, max_bytes=9)
        cycles = 200
        for cycle in range(cycles):
            for i in range(3):
                await _with_timeout(queue.put(f"{cycle}-{i}", size=1))
            for _ in range(3):
                await _with_timeout(queue.get())
                queue.task_done()
            await _with_timeout(queue.join())
            await queue.close()
            await queue.reopen()

        status = queue.status()
        assert status["items"] == 0
        assert status["bytes"] == 0
        assert status["unfinished"] == 0
        assert status["accepted"] == cycles * 3

        await asyncio.sleep(0.05)  # allow any scheduled notify tasks to finish
        leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        assert leftover == []

    asyncio.run(scenario())


def test_construction_and_size_validation_reject_invalid_bounds() -> None:
    """Constructor and per-item validation guard the "no unbounded queue"
    AC directly: bad limits/overload modes/oversized items must be rejected
    up front rather than silently accepted and later causing unbounded
    growth.
    """
    with pytest.raises(ValueError):
        AsyncBoundedQueue(0)
    with pytest.raises(ValueError):
        AsyncBoundedQueue(1, max_bytes=-1)
    with pytest.raises(ValueError):
        AsyncBoundedQueue(1, overload="retry")

    async def scenario() -> None:
        queue = AsyncBoundedQueue(1, max_bytes=4)
        with pytest.raises(ValueError):
            await queue.put("too-big", size=5)
        with pytest.raises(ValueError):
            await queue.put("negative", size=-1)

    asyncio.run(scenario())


def test_put_on_already_closed_queue_raises_immediately() -> None:
    """A producer racing a shutdown must fail fast with QueueClosed instead
    of silently enqueuing into a queue nobody will ever drain again.
    """

    async def scenario() -> None:
        queue = AsyncBoundedQueue(2)
        await queue.close()
        with pytest.raises(QueueClosed):
            await queue.put("too-late")

    asyncio.run(scenario())


def test_put_woken_by_close_while_waiting_raises_queue_closed() -> None:
    """A producer already parked waiting for capacity, when the queue is
    closed out from under it (e.g. an ordered shutdown), must be woken with
    QueueClosed rather than left hanging or silently dropped.
    """

    async def scenario() -> None:
        queue = AsyncBoundedQueue(1)
        await queue.put("filler")

        waiter = asyncio.create_task(queue.put("blocked"))
        await asyncio.sleep(0.01)
        assert not waiter.done()

        await queue.close()
        with pytest.raises(QueueClosed):
            await _with_timeout(waiter)

    asyncio.run(scenario())


def test_put_timeout_budget_already_exhausted_raises_backpressure() -> None:
    """A near-zero timeout on a full queue must resolve to a bounded
    BackpressureError promptly (no indefinite wait) even in the branch where
    the remaining budget is computed as already spent.
    """

    async def scenario() -> None:
        from simplicio_loop.async_bounded_queue import BackpressureError

        queue = AsyncBoundedQueue(1)
        await queue.put("filler")
        with pytest.raises(BackpressureError) as error:
            await _with_timeout(queue.put("late", timeout=0.0), seconds=1.0)
        assert error.value.receipt["reason"] == "timeout"

    asyncio.run(scenario())


def test_extra_task_done_call_raises_instead_of_underflowing() -> None:
    """A buggy/duplicate task_done() call (e.g. a crashed consumer restarted
    and both the old and new code path account for the same item) must
    raise instead of silently underflowing `unfinished` into a state that
    would make join() return too early and hide real outstanding work.
    """

    async def scenario() -> None:
        queue = AsyncBoundedQueue(1)
        await queue.put("one")
        await queue.get()
        queue.task_done()
        with pytest.raises(ValueError):
            queue.task_done()

    asyncio.run(scenario())


def test_cancelled_waiting_consumer_does_not_leak_capacity_or_hang_close() -> None:
    """A consumer parked in get() (queue empty) gets killed; close() must
    still be able to wake/terminate any other waiters without hanging, and
    the queue must remain usable afterwards for a fresh producer/consumer.
    """

    async def scenario() -> None:
        queue = AsyncBoundedQueue(1)

        async def waiting_consumer() -> None:
            await queue.get()

        consumer_task = asyncio.create_task(waiting_consumer())
        await asyncio.sleep(0.01)
        assert not consumer_task.done()
        consumer_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer_task

        # close() must not hang even though a waiter was just cancelled.
        await _with_timeout(queue.close())
        with pytest.raises(QueueClosed):
            await _with_timeout(queue.get())

        await queue.reopen()
        await _with_timeout(queue.put("alive"))
        value, _, _ = await _with_timeout(queue.get())
        queue.task_done()
        assert value == "alive"

    asyncio.run(scenario())
