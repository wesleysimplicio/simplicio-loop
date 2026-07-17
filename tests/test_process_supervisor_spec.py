import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

from simplicio_loop.process_supervisor import (
    ProcessLease,
    ProcessSpec,
    ProcessSpecError,
    PythonProcessAdapter,
)


def test_process_spec_is_structured_and_allowlisted() -> None:
    with tempfile.TemporaryDirectory() as directory:
        spec = ProcessSpec(
            (sys.executable, "-c", "print('ok')"),
            cwd=str(Path(directory).resolve()),
            env={"SIMPLICIO_TEST": "yes"},
            env_allowlist=("SIMPLICIO_TEST",),
            idempotency_key="one",
        )
        assert spec.to_dict()["shell"] is False
        assert spec.spec_hash
        with pytest.raises(ProcessSpecError):
            ProcessSpec(("echo",), shell=True)
        with pytest.raises(ProcessSpecError):
            ProcessSpec(("echo",), env={"NO": "x"}, env_allowlist=())


def test_lease_heartbeat_expiry_and_cancel() -> None:
    lease = ProcessLease("lease-1", "spec-1", ttl_seconds=5, expires_at=10)
    assert not lease.expired(now=9)
    assert lease.heartbeat(now=20) == 25
    assert not lease.expired(now=24)
    assert lease.expired(now=25)
    lease.cancel()
    assert lease.state == "cancelled"


def test_adapter_runs_argv_and_bounds_output() -> None:
    async def scenario() -> None:
        adapter = PythonProcessAdapter()
        spec = ProcessSpec(
            (sys.executable, "-c", "print('x' * 100)"),
            max_output_bytes=10,
        )
        result = await adapter.run(spec)
        assert result.returncode == 0
        assert len(result.stdout) == 10
        assert result.truncated

        failed = await adapter.run(
            ProcessSpec((sys.executable, "-c", "raise SystemExit(3)"))
        )
        assert failed.returncode == 3

    asyncio.run(scenario())


def test_adapter_classifies_timeout_and_missing_executable() -> None:
    async def scenario() -> None:
        adapter = PythonProcessAdapter()
        timeout = await adapter.run(
            ProcessSpec(
                (sys.executable, "-c", "import time; time.sleep(2)"),
                timeout_seconds=0.01,
            )
        )
        assert timeout.timed_out
        assert timeout.error_code == "deadline_exceeded"

        missing = await adapter.run(ProcessSpec(("simplicio-no-such-executable",)))
        assert missing.error_code == "executable_not_found"

    asyncio.run(scenario())


def test_adapter_cancellation_is_classified() -> None:
    async def scenario() -> None:
        task = asyncio.create_task(
            PythonProcessAdapter().run(
                ProcessSpec((sys.executable, "-c", "import time; time.sleep(2)"))
            )
        )
        await asyncio.sleep(0.02)
        task.cancel()
        result = await task
        assert result.cancelled
        assert result.error_code == "cancelled"

    asyncio.run(scenario())
