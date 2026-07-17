import asyncio

import pytest

from simplicio_loop.loop_runtime import LoopRuntime


def test_run_sync_and_validation() -> None:
    async def value() -> str:
        return "ok"

    assert LoopRuntime(2).run_sync(value) == "ok"
    with pytest.raises(ValueError):
        LoopRuntime(0)


def test_bounded_spawn_and_results() -> None:
    async def scenario() -> None:
        runtime = LoopRuntime(max_concurrency=2)
        active = 0
        peak = 0
        lock = asyncio.Lock()

        async def work(value: int) -> int:
            nonlocal active, peak
            async with lock:
                active += 1
                peak = max(peak, active)
            await asyncio.sleep(0.005)
            async with lock:
                active -= 1
            return value * 2

        tasks = [runtime.spawn(work, index) for index in range(8)]
        assert await asyncio.gather(*tasks) == list(range(0, 16, 2))
        assert peak <= 2
        assert runtime.active_tasks == 0
        await runtime.shutdown()

    asyncio.run(scenario())


def test_timeout_cancels_operation() -> None:
    async def scenario() -> None:
        runtime = LoopRuntime()
        cancelled = asyncio.Event()

        async def slow() -> None:
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        with pytest.raises(asyncio.TimeoutError):
            await runtime.run(slow, timeout=0.01)
        await asyncio.wait_for(cancelled.wait(), timeout=0.5)
        assert runtime.active_tasks == 0
        await runtime.shutdown()

    asyncio.run(scenario())


def test_shutdown_is_idempotent_and_rejects_new_work() -> None:
    async def scenario() -> None:
        runtime = LoopRuntime()
        started = asyncio.Event()
        release = asyncio.Event()

        async def waiting() -> None:
            started.set()
            await release.wait()

        task = runtime.spawn(waiting)
        await asyncio.wait_for(started.wait(), timeout=0.5)
        runtime.request_shutdown()
        await runtime.shutdown()
        await runtime.shutdown()
        assert task.cancelled()
        assert runtime.closed
        assert runtime.shutdown_requested
        with pytest.raises(RuntimeError):
            await runtime.run(waiting)

    asyncio.run(scenario())


def test_operation_errors_propagate() -> None:
    async def scenario() -> None:
        runtime = LoopRuntime()

        async def fail() -> None:
            raise LookupError("expected")

        with pytest.raises(LookupError, match="expected"):
            await runtime.run(fail)
        await runtime.shutdown()

    asyncio.run(scenario())


def test_run_sync_rejects_active_event_loop() -> None:
    async def scenario() -> None:
        runtime = LoopRuntime()

        async def value() -> int:
            return 1

        with pytest.raises(
            RuntimeError, match="active event loop"
        ):
            runtime.run_sync(value)

    asyncio.run(scenario())
