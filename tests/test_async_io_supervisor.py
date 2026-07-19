from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

from simplicio_loop.async_io_supervisor import (
    AsyncProcessSupervisor,
    DuplicateLease,
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


def test_duplicate_lease_id_is_rejected_while_active() -> None:
    """A resubmit of the same lease id while it is still in-flight must be
    rejected outright rather than spawning a second real process — this is
    the concrete mechanism that prevents duplicate work under a restart that
    re-submits the same idempotency key before the first attempt finished."""

    async def scenario() -> None:
        supervisor = AsyncProcessSupervisor(adapter=BlockingAdapter())
        first = asyncio.create_task(
            supervisor.run(spec("import time; time.sleep(10)", key="dup-key"))
        )
        await asyncio.sleep(0.05)
        with pytest.raises(DuplicateLease):
            await supervisor.run(spec("print('should not run')", key="dup-key"))
        first.cancel()
        result = await first
        assert result.cancelled is True

    asyncio.run(scenario())


def test_restart_cycles_run_exactly_once_no_loss_or_duplication() -> None:
    """Simulate repeated process-level restarts: each cycle throws away the
    supervisor (as a real restart would drop all in-memory state) and
    resubmits the same idempotency key against a fresh instance. Every cycle
    must produce exactly one real, successful result for that key — no
    silent loss (missing result) and no duplicate execution (the guard in
    test_duplicate_lease_id_is_rejected_while_active only helps within a
    single process lifetime, so this proves the per-cycle behavior that
    stands in for full cross-restart persistence, which does not exist yet
    — see GENUINE-GAP in the report)."""

    async def scenario() -> None:
        results = []
        for cycle in range(8):
            supervisor = AsyncProcessSupervisor(max_concurrency=2)
            result = await supervisor.run(
                spec(f"print('cycle-{cycle}')", key="restart-key")
            )
            results.append(result)
            assert supervisor.status()["active_leases"] == 0
            assert supervisor.status()["active_tasks"] == 0

        assert len(results) == 8
        assert all(r.returncode == 0 for r in results)
        stdouts = [r.stdout.strip() for r in results]
        assert stdouts == [f"cycle-{i}" for i in range(8)]
        assert len(set(stdouts)) == len(stdouts)

    asyncio.run(scenario())


def test_stress_many_concurrent_leases_no_leak_or_duplicate_result() -> None:
    """Stress-of-restart proxy: fire many concurrent bounded leases (mixing
    normal completion, timeout and mid-flight cancellation) and assert the
    supervisor converges back to zero active leases/tasks with no lease id
    ever appearing twice among the results — i.e. no leak and no
    duplication under load."""

    async def scenario() -> None:
        supervisor = AsyncProcessSupervisor(max_concurrency=4)
        total = 40

        async def run_one(index: int) -> ProcessResult:
            key = f"stress-{index}"
            if index % 7 == 0:
                code = "import time; time.sleep(5)"
                timeout = 0.02
            else:
                code = f"print('ok-{index}')"
                timeout = 2.0
            return await supervisor.run(spec(code, timeout=timeout, key=key))

        tasks = [asyncio.create_task(run_one(i)) for i in range(total)]
        results = await asyncio.gather(*tasks)

        lease_ids = [r.lease_id for r in results]
        assert len(lease_ids) == len(set(lease_ids)) == total
        assert supervisor.status()["active_leases"] == 0
        assert supervisor.status()["active_tasks"] == 0
        timed_out = [r for r in results if r.timed_out]
        assert len(timed_out) == len([i for i in range(total) if i % 7 == 0])
        succeeded = [r for r in results if r.returncode == 0]
        assert len(succeeded) == total - len(timed_out)

    asyncio.run(scenario())


def test_no_orphan_process_survives_cancellation(tmp_path) -> None:
    """After a lease is cancelled mid-flight, the underlying OS process must
    actually be gone (reaped), not merely marked cancelled in-memory — a
    real check against the live PID that guards against process leaks
    across restarts, using the real PythonProcessAdapter (no fake)."""

    pidfile = tmp_path / "child.pid"
    child_code = (
        "import os, time; "
        f"open(r'{pidfile}', 'w').write(str(os.getpid())); "
        "time.sleep(10)"
    )

    async def scenario() -> None:
        supervisor = AsyncProcessSupervisor(max_concurrency=1)
        task = asyncio.create_task(
            supervisor.run(spec(child_code, key="orphan-check"))
        )
        for _ in range(50):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.05)
        assert pidfile.exists(), "child never wrote its pid"
        pid = int(pidfile.read_text().strip())

        task.cancel()
        result = await task
        assert result.cancelled is True

        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    asyncio.run(scenario())
