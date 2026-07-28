from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from simplicio_loop.fast_integration import FastConfig
from simplicio_loop.fast_resident import (
    FastResidentService,
    FastServiceCrashed,
    FastServiceError,
    FastServiceRegistry,
    Lifecycle,
    PythonMemoryBackend,
)


class Backend:
    engine = "rust"

    def __init__(self, state=None):
        self.state = state if state is not None else {}
        self.closed = False

    async def start(self, task):
        del task
        self.state["starts"] = self.state.get("starts", 0) + 1
        return {"generation": f"g{self.state['starts']}", "engine": self.engine}

    async def query(self, query, *, max_results):
        self.state["calls"] = self.state.get("calls", 0) + 1
        return {"schema": "simplicio.loop-fast-resident/v1", "status": "READY",
                "generation": f"g{self.state['starts']}", "engine": self.engine,
                "results": [{"path": "app.py", "content": query, "score": max_results}],
                "result_hash": "sha256:test"}

    async def close(self):
        self.closed = True


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.mark.parametrize("slots", (1, 5, 20))
def test_one_service_and_generation_are_shared_by_all_slots(tmp_path, slots):
    async def scenario():
        state = {}
        registry = FastServiceRegistry()
        config = FastConfig(command=("fast",))
        services = await asyncio.gather(*(
            registry.acquire(tmp_path, task="inspect app", config=config,
                             backend_factory=lambda: Backend(state))
            for _ in range(slots)
        ))
        assert len({id(service) for service in services}) == 1
        assert len({service.generation for service in services}) == 1
        assert state["starts"] == 1
        results = await asyncio.gather(*(
            service.query(f"slot-{index}", "app", max_results=3)
            for index, service in enumerate(services)
        ))
        assert len(results) == slots
        assert all(result["confirmed"] for result in results)
        assert services[0].metrics["cold_starts"] == 1
        assert services[0].metrics["warm_reuses"] == slots - 1
        await registry.drain()
        assert services[0].lifecycle == Lifecycle.STOPPED

    run(scenario())


def test_crash_reconnect_retries_only_unconfirmed_request(tmp_path):
    class CrashOnce(Backend):
        async def query(self, query, *, max_results):
            if not self.state.get("crashed"):
                self.state["crashed"] = True
                raise FastServiceCrashed("process exited")
            return await super().query(query, max_results=max_results)

    async def scenario():
        state = {}
        service = FastResidentService(tmp_path, lambda: CrashOnce(state))
        await service.start("app")
        result = await service.query("request-1", "app")
        assert result["confirmed"] is True
        assert service.metrics["reconnects"] == 1
        assert service.metrics["rebuilds"] == 1
        with pytest.raises(FastServiceError, match="already confirmed"):
            await service.query("request-1", "app")

    run(scenario())


def test_timeout_and_cancellation_are_visible(tmp_path):
    class Slow(Backend):
        async def query(self, query, *, max_results):
            await asyncio.sleep(1)
            return await super().query(query, max_results=max_results)

    async def scenario():
        service = FastResidentService(tmp_path, lambda: Slow(), timeout_seconds=0.01)
        await service.start("app")
        with pytest.raises(asyncio.TimeoutError):
            await service.query("timeout", "app")
        assert service.metrics["timeouts"] == 1

        service.timeout_seconds = 2
        pending = asyncio.create_task(service.query("cancel", "app"))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert service.metrics["cancellations"] == 1

    run(scenario())


def test_python_fallback_preserves_contract_without_offsets(tmp_path):
    (tmp_path / "app.py").write_text("def fast_service():\n    return 'ready'\n", encoding="utf-8")

    async def scenario():
        async def unavailable():
            raise OSError("Rust Fast unavailable")

        service = FastResidentService(
            tmp_path, unavailable, fallback_factory=lambda: PythonMemoryBackend(tmp_path)
        )
        receipt = await service.start("fast service")
        result = await service.query("fallback-query", "fast service")
        assert receipt["engine"] == "python"
        assert receipt["metrics"]["fallbacks"] == 1
        assert result["schema"] == "simplicio.loop-fast-resident/v1"
        assert result["results"][0]["path"] == "app.py"
        assert "offset" not in repr(result).lower()
        assert ".sfast" not in repr(result).lower()

    run(scenario())


def test_internal_offsets_fail_closed(tmp_path):
    class Leaking(Backend):
        async def query(self, query, *, max_results):
            return {"results": [{"path": "app.py", "offset": 10}]}

    async def scenario():
        service = FastResidentService(tmp_path, lambda: Leaking())
        await service.start("app")
        with pytest.raises(FastServiceError, match="offset"):
            await service.query("leak", "app")

    run(scenario())


def test_python_and_rust_backends_preserve_public_result_contract(tmp_path):
    (tmp_path / "app.py").write_text("resident fast service\n", encoding="utf-8")

    async def scenario():
        rust = FastResidentService(tmp_path, lambda: Backend())
        python = FastResidentService(tmp_path, lambda: PythonMemoryBackend(tmp_path))
        await rust.start("resident fast")
        await python.start("resident fast")
        rust_result = await rust.query("rust", "resident fast")
        python_result = await python.query("python", "resident fast")
        public = {"schema", "status", "generation", "engine", "results",
                  "result_hash", "request_id", "confirmed"}
        assert set(rust_result) == public
        assert set(python_result) == public
        assert rust_result["engine"] == "rust"
        assert python_result["engine"] == "python"

    run(scenario())


def test_checked_in_receipt_hash_is_reproducible():
    fixture = (
        __import__("pathlib").Path(__file__).parent
        / "fixtures" / "fast_resident_receipt_804.json"
    )
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    expected = payload.pop("evidence_hash")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    assert expected == "sha256:" + hashlib.sha256(raw).hexdigest()


def test_registry_status_and_idempotent_drain(tmp_path):
    async def scenario():
        registry = FastServiceRegistry()
        service = await registry.acquire(
            tmp_path, task="app", config=FastConfig(command=("fast",)),
            backend_factory=lambda: Backend(),
        )
        status = registry.status()
        assert status[0]["lifecycle"] == "ready"
        assert status[0]["observability"]["rss_bytes"] is None
        first = await registry.drain()
        second = await service.drain()
        assert first[0]["lifecycle"] == "stopped"
        assert second["status"] == "STOPPED"

    run(scenario())
