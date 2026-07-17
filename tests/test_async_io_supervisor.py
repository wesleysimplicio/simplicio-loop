from __future__ import annotations

import asyncio
import sys
import time

import pytest

from simplicio_loop.async_io_supervisor import (
    AsyncProcessSupervisor,
    SupervisorClosed,
)
from simplicio_loop.process_supervisor import ProcessLease, ProcessResult, ProcessSpec


def spec(code: str, *, timeout: float = 2.0, key: str = "") -> ProcessSpec:
    return ProcessSpec(
        argv=(sys.executable, "-c", code),
        timeout_seconds=timeout,
        idempotency_key=key,
    )


class BlockingAdapter:
    async def run(self, process_spec: ProcessSpec, *, lease: ProcessLease) -> ProcessResult:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            return ProcessResult(
                None, cancelled=True, error_code="cancelled", lease_id=lease.lease_id
            )
        return ProcessResult(0, lease_id=lease.lease_id)


def test_real_async_process_success() -> None:
    async def scenario() -> None:
        supervisor = AsyncProcessSupervisor(max_concurrency=1)
        result = await supervisor.run(spec("print('ok')"))
        assert result.returncode == 0 and result.stdout.strip() == "ok"
        assert supervisor.status()["active_tasks"] == 0

    asyncio.run(scenario())


def test_timeout_is_classified_and_process_is_reaped() -> None:
    async def scenario() -> None:
        supervisor = AsyncProcessSupervisor()
        result = await supervisor.run(
            spec("import time; time.sleep(1)", timeout=0.01)
        )
        assert result.timed_out is True
        assert result.error_code == "deadline_exceeded"
        assert supervisor.status()["active_leases"] == 0

    asyncio.run(scenario())


def test_cancellation_returns_cancelled_result() -> None:
    async def scenario() -> None:
        supervisor = AsyncProcessSupervisor(adapter=BlockingAdapter())
        task = asyncio.create_task(
            supervisor.run(spec("import time; time.sleep(10)", timeout=20))
        )
        await asyncio.sleep(0.05)
        task.cancel()
        result = await task
        assert result.cancelled is True
        assert result.error_code == "cancelled"

    asyncio.run(scenario())


def test_lease_recovery_cancels_expired_run() -> None:
    async def scenario() -> None:
        supervisor = AsyncProcessSupervisor(adapter=BlockingAdapter())
        lease = ProcessLease("expired", "hash", ttl_seconds=30)
        lease.expires_at = time.monotonic() - 1
        task = asyncio.create_task(
            supervisor.run(spec("import time; time.sleep(10)"), lease=lease)
        )
        await asyncio.sleep(0.05)
        assert await supervisor.recover_expired(now=time.monotonic()) == ["expired"]
        result = await task
        assert result.cancelled is True
        assert result.lease_id == "expired"

    asyncio.run(scenario())


def test_shutdown_drains_and_rejects_new_work() -> None:
    async def scenario() -> None:
        supervisor = AsyncProcessSupervisor(adapter=BlockingAdapter())
        task = asyncio.create_task(
            supervisor.run(spec("import time; time.sleep(10)"))
        )
        await asyncio.sleep(0.05)
        status = await supervisor.shutdown()
        assert status["draining"] is True
        assert status["active_tasks"] == 0
        await task
        with pytest.raises(SupervisorClosed):
            await supervisor.run(spec("print('no')"))

    asyncio.run(scenario())


def test_concurrency_is_bounded() -> None:
    class FakeAdapter:
        async def run(self, process_spec: ProcessSpec, *, lease: ProcessLease) -> ProcessResult:
            await asyncio.sleep(0.08 if "sleep" in process_spec.argv[-1] else 0)
            return ProcessResult(0, stdout="ok", lease_id=lease.lease_id)

    async def scenario() -> None:
        supervisor = AsyncProcessSupervisor(
            adapter=FakeAdapter(), max_concurrency=1
        )
        first = asyncio.create_task(
            supervisor.run(spec("import time; time.sleep(0.08)", key="first"))
        )
        await asyncio.sleep(0.01)
        second = asyncio.create_task(
            supervisor.run(spec("print('second')", key="second"))
        )
        await asyncio.sleep(0.01)
        assert supervisor.status()["active_tasks"] == 2
        assert supervisor.status()["semaphore_available"] == 0
        await asyncio.gather(first, second)
        assert supervisor.status()["active_tasks"] == 0

    asyncio.run(scenario())
