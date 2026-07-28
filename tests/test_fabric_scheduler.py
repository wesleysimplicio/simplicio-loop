from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

from simplicio_loop.fabric_scheduler import (
    AsyncFabricScheduler,
    FabricJob,
    ReplayError,
    TransitionJournal,
    replay_terminal,
)
from simplicio_loop.process_supervisor import ProcessSpec, PythonProcessAdapter


def test_capacity_backpressure_and_stress_matrix(tmp_path: Path) -> None:
    async def scenario(count: int, capacity: int) -> dict:
        active = 0
        observed = 0
        release = asyncio.Event()

        async def work(value: int) -> int:
            nonlocal active, observed
            active += 1
            observed = max(observed, active)
            await release.wait()
            active -= 1
            return value

        scheduler = AsyncFabricScheduler(
            max_running=capacity,
            queue_capacity=max(1, count - capacity),
            capability_limits={"cpu": capacity},
            journal_path=str(tmp_path / ("stress-%d-%d.jsonl" % (count, capacity))),
        )
        await scheduler.start()
        futures = []
        producers = [
            asyncio.create_task(
                scheduler.submit(FabricJob(str(i), lambda i=i: work(i), capability="cpu"))
            )
            for i in range(count)
        ]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()
        for producer in producers:
            futures.append(await producer)
        assert sorted(await asyncio.gather(*futures)) == list(range(count))
        receipt = await scheduler.shutdown()
        assert observed <= capacity
        assert receipt["max_observed_running"] <= capacity
        assert receipt["queued"] == receipt["running"] == 0
        return receipt

    for count, capacity in ((1, 1), (6, 2), (64, 6)):
        receipt = asyncio.run(scenario(count, capacity))
        assert len(receipt["terminal"]) == count
    assert receipt["producer_waits"] > 0


def test_capability_and_resource_admission_lanes_are_bounded(tmp_path: Path) -> None:
    async def scenario() -> dict:
        gate = asyncio.Event()

        async def work(value: int) -> int:
            await gate.wait()
            return value

        scheduler = AsyncFabricScheduler(
            max_running=1,
            queue_capacity=10,
            capability_queue_limits={"cpu": 1},
            resource_queue_limits={"repo": 1},
            journal_path=str(tmp_path / "lane-bounds.jsonl"),
        )
        first = await scheduler.submit(
            FabricJob("first", lambda: work(1), capability="cpu", resources=frozenset({"repo"}))
        )
        await asyncio.sleep(0)
        second = await scheduler.submit(
            FabricJob("second", lambda: work(2), capability="cpu", resources=frozenset({"repo"}))
        )
        third_submit = asyncio.create_task(
            scheduler.submit(
                FabricJob("third", lambda: work(3), capability="cpu", resources=frozenset({"repo"}))
            )
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not third_submit.done()
        gate.set()
        third = await third_submit
        assert await asyncio.gather(first, second, third) == [1, 2, 3]
        return await scheduler.shutdown()

    receipt = asyncio.run(scenario())
    assert receipt["producer_waits"] >= 1
    assert receipt["capability_queue_limits"] == {"cpu": 1}
    assert receipt["resource_queue_limits"] == {"repo": 1}


def test_reads_parallelize_and_conflicting_writes_serialize(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[tuple[str, float, float]], dict]:
        intervals: list[tuple[str, float, float]] = []

        async def work(name: str, delay: float = 0.035) -> str:
            start = time.monotonic()
            await asyncio.sleep(delay)
            intervals.append((name, start, time.monotonic()))
            return name

        async with AsyncFabricScheduler(
            max_running=4,
            queue_capacity=8,
            journal_path=str(tmp_path / "resources.jsonl"),
        ) as scheduler:
            futures = [
                await scheduler.submit(
                    FabricJob("read-a", lambda: work("read-a"), resources=frozenset({"repo"}))
                ),
                await scheduler.submit(
                    FabricJob("read-b", lambda: work("read-b"), resources=frozenset({"repo"}))
                ),
                await scheduler.submit(
                    FabricJob(
                        "write-a",
                        lambda: work("write-a"),
                        resources=frozenset({"repo"}),
                        mode="write",
                    )
                ),
                await scheduler.submit(
                    FabricJob(
                        "write-b",
                        lambda: work("write-b"),
                        resources=frozenset({"repo"}),
                        mode="build",
                    )
                ),
            ]
            assert set(await asyncio.gather(*futures)) == {
                "read-a",
                "read-b",
                "write-a",
                "write-b",
            }
            status = scheduler.status()
        return intervals, status

    intervals, status = asyncio.run(scenario())
    by_name = {name: (start, end) for name, start, end in intervals}
    assert by_name["read-a"][0] < by_name["read-b"][1]
    assert by_name["read-b"][0] < by_name["read-a"][1]
    assert by_name["write-a"][0] >= max(by_name["read-a"][1], by_name["read-b"][1])
    assert by_name["write-b"][0] >= by_name["write-a"][1]
    assert status["max_observed_running"] == 2


def test_failure_is_isolated_and_every_job_becomes_terminal(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[object], dict]:
        scheduler = AsyncFabricScheduler(
            max_running=3, queue_capacity=6, journal_path=str(tmp_path / "failure.jsonl")
        )

        async def fail() -> None:
            raise RuntimeError("expected failure")

        async def succeed(value: int) -> int:
            await asyncio.sleep(0)
            return value

        futures = [
            await scheduler.submit(FabricJob("ok-1", lambda: succeed(1))),
            await scheduler.submit(FabricJob("bad", fail)),
            await scheduler.submit(FabricJob("ok-2", lambda: succeed(2))),
        ]
        outcomes = await asyncio.gather(*futures, return_exceptions=True)
        receipt = await scheduler.shutdown()
        return outcomes, receipt

    outcomes, receipt = asyncio.run(scenario())
    assert outcomes[0] == 1 and outcomes[2] == 2
    assert isinstance(outcomes[1], RuntimeError)
    assert receipt["terminal"] == {
        "bad": "failed",
        "ok-1": "succeeded",
        "ok-2": "succeeded",
    }


def test_priority_aging_prevents_starvation(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[str], dict]:
        order: list[str] = []
        gate = asyncio.Event()
        scheduler = AsyncFabricScheduler(
            max_running=1,
            queue_capacity=8,
            aging_seconds=0.01,
            journal_path=str(tmp_path / "aging.jsonl"),
        )

        async def blocker() -> None:
            await gate.wait()

        async def record(name: str) -> str:
            order.append(name)
            return name

        first = await scheduler.submit(FabricJob("blocker", blocker, priority=10))
        low = await scheduler.submit(FabricJob("low", lambda: record("low"), priority=0))
        await asyncio.sleep(0.025)
        high = await scheduler.submit(FabricJob("high", lambda: record("high"), priority=1))
        gate.set()
        await asyncio.gather(first, low, high)
        receipt = await scheduler.shutdown()
        return order, receipt

    order, receipt = asyncio.run(scenario())
    assert order == ["low", "high"]
    assert receipt["starvation_promotions"] >= 1


def test_cancel_storm_cancels_pending_and_running_jobs(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[object], dict]:
        started = asyncio.Event()

        async def wait_forever() -> None:
            started.set()
            await asyncio.Event().wait()

        scheduler = AsyncFabricScheduler(
            max_running=3, queue_capacity=20, journal_path=str(tmp_path / "cancel.jsonl")
        )
        futures = [
            await scheduler.submit(FabricJob("cancel-%d" % i, wait_forever))
            for i in range(12)
        ]
        await started.wait()
        await asyncio.gather(*(scheduler.cancel("cancel-%d" % i) for i in range(12)))
        outcomes = await asyncio.gather(*futures, return_exceptions=True)
        receipt = await scheduler.shutdown()
        return outcomes, receipt

    outcomes, receipt = asyncio.run(scenario())
    assert all(isinstance(item, asyncio.CancelledError) for item in outcomes)
    assert set(receipt["terminal"].values()) == {"cancelled"}
    assert receipt["queued"] == receipt["running"] == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX pid probe")
def test_process_cancellation_reaps_child(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, dict]:
        adapter = PythonProcessAdapter()
        spawned: asyncio.Future[int] = asyncio.get_running_loop().create_future()

        async def process_work():
            spec = ProcessSpec(
                argv=(sys.executable, "-c", "import time; time.sleep(30)"),
                timeout_seconds=60,
            )
            return await adapter.run(
                spec,
                on_spawned=lambda process: spawned.set_result(process.pid),
            )

        scheduler = AsyncFabricScheduler(
            max_running=1, queue_capacity=1, journal_path=str(tmp_path / "process.jsonl")
        )
        result_future = await scheduler.submit(FabricJob("process", process_work))
        pid = await asyncio.wait_for(spawned, timeout=5)
        await scheduler.cancel("process")
        with pytest.raises(asyncio.CancelledError):
            await result_future
        receipt = await scheduler.shutdown()
        return pid, receipt

    pid, receipt = asyncio.run(scenario())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert receipt["terminal"]["process"] == "cancelled"


def test_replay_reconstructs_terminal_and_detects_tampering(tmp_path: Path) -> None:
    path = tmp_path / "replay.jsonl"

    async def scenario() -> None:
        async with AsyncFabricScheduler(
            max_running=1, queue_capacity=2, journal_path=str(path)
        ) as scheduler:
            future = await scheduler.submit(
                FabricJob("replayed", lambda: asyncio.sleep(0, result="ok"))
            )
            assert await future == "ok"

    asyncio.run(scenario())
    assert replay_terminal(str(path)) == {"replayed": "succeeded"}
    rows = TransitionJournal(str(path)).rows
    assert [row["state"] for row in rows] == ["queued", "ready", "running", "succeeded"]

    path.write_text(path.read_text(encoding="utf-8").replace('"succeeded"', '"failed"', 1), encoding="utf-8")
    with pytest.raises(ReplayError):
        replay_terminal(str(path))


def test_acceptance_receipt_is_content_addressed() -> None:
    root = Path(__file__).resolve().parents[1]
    receipt_path = root / "tests" / "fixtures" / "fabric_scheduler_806_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    declared = receipt.pop("receipt_sha")
    canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == declared
    assert receipt["classification"] == "MEASURED"
    assert receipt["performance_metrics"] is None
    for reference in receipt["criteria"].values():
        path, test_name = reference.split("::", 1)
        source = (root / path).read_text(encoding="utf-8")
        assert ("def %s(" % test_name) in source
