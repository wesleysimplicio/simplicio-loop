"""Real subprocess restart/recovery proofs for issue #509."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from simplicio_loop.async_io_supervisor import AsyncProcessSupervisor
from simplicio_loop.process_supervisor import ProcessSpec


def _spec(marker: Path, key: str, *, delay: float = 0.0) -> ProcessSpec:
    code = (
        "from pathlib import Path; import time; "
        f"time.sleep({delay!r}); Path({str(marker)!r}).open('a', encoding='utf-8').write({key!r} + '\\n'); "
        f"print({key!r})"
    )
    return ProcessSpec(
        argv=(sys.executable, "-c", code),
        timeout_seconds=5,
        idempotency_key=key,
    )


def test_real_api_recreates_supervisor_without_duplicate_side_effect(tmp_path: Path) -> None:
    async def scenario() -> None:
        state = tmp_path / "supervisor-state.json"
        marker = tmp_path / "side-effects.log"
        first = AsyncProcessSupervisor(max_concurrency=1, state_path=str(state))
        result_one = await first.run(_spec(marker, "job-1"))
        assert result_one.stdout.strip() == "job-1"
        assert first.status()["persisted_outcomes"] == 1

        restarted = AsyncProcessSupervisor(max_concurrency=1, state_path=str(state))
        result_two = await restarted.run(_spec(marker, "job-1"))
        assert result_two.stdout == result_one.stdout
        assert marker.read_text(encoding="utf-8").splitlines() == ["job-1"]
        assert restarted.status()["active_leases"] == 0

    asyncio.run(scenario())


def test_restart_recovers_abandoned_lease_and_allows_idempotent_retry(tmp_path: Path) -> None:
    async def scenario() -> None:
        state = tmp_path / "supervisor-state.json"
        marker = tmp_path / "recovery.log"
        running = AsyncProcessSupervisor(max_concurrency=1, state_path=str(state))
        task = asyncio.create_task(running.run(_spec(marker, "job-2", delay=2)))
        await asyncio.sleep(0.1)

        restarted = AsyncProcessSupervisor(max_concurrency=1, state_path=str(state))
        assert restarted.status()["recovered_leases"]
        await running.shutdown()
        await task

        retried = await restarted.run(_spec(marker, "job-2"))
        assert retried.stdout.strip() == "job-2"
        assert marker.read_text(encoding="utf-8").splitlines() == ["job-2"]
        assert restarted.status()["active_leases"] == 0

    asyncio.run(scenario())
