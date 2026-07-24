import asyncio

import pytest

from simplicio_loop.inference_admission import (
    AdmissionJob,
    AdmissionRejected,
    CapacityLimits,
    FairAdmissionController,
    InferenceCoordinator,
    InferenceRequest,
)


def request(**overrides):
    values = dict(model_identity="model-1", backend_identity="backend-1", stable_prefix_generation="prefix-1", canonical_request_hash="request-1", tool_registry_generation="tools-1", authority_scope="tenant-a", privacy_scope="user-a", side_effect_free=True)
    values.update(overrides)
    return InferenceRequest(**values)


def job(job_id, client, priority="background"):
    return AdmissionJob(job_id, client, "session-1", priority=priority)


def test_independent_caps_defer_and_reject_without_growth():
    controller = FairAdmissionController(CapacityLimits(max_runnable=1, max_active_workers=1, max_inference_requests=1, max_backend_slots=1, max_queue=2))
    assert controller.submit(job("a", "hot")).state == "admitted"
    assert controller.submit(job("b", "cold")).state == "deferred"
    assert controller.submit(job("c", "cold")).state == "deferred"
    rejected = controller.submit(job("d", "other"))
    assert rejected.state == "rejected"
    assert rejected.reason == "queue_saturated"
    assert controller.status()["queued"] == 2
    assert controller.release("a")
    assert controller.next().job_id == "b"
    assert controller.status()["usage"]["active_workers"] == 1


def test_priority_and_aging_serve_multiple_clients():
    controller = FairAdmissionController(CapacityLimits(max_runnable=1, max_active_workers=1, max_inference_requests=1, max_backend_slots=1, max_queue=8), aging_ticks=1)
    assert controller.submit(job("active", "active")).state == "admitted"
    controller.submit(job("hot-1", "hot", "background"))
    controller.submit(job("cold-1", "cold", "interactive"))
    controller.release("active")
    assert controller.next().client_id == "cold"
    controller.release("cold-1")
    assert controller.next().client_id == "hot"
    assert controller.status()["starvation_preventions"] >= 0


def test_equivalence_key_has_safety_boundaries():
    base = request()
    assert base.equivalence_key() == request().equivalence_key()
    assert base.equivalence_key() != request(authority_scope="tenant-b").equivalence_key()
    assert base.equivalence_key() != request(privacy_scope="user-b").equivalence_key()
    assert base.equivalence_key() != request(tool_registry_generation="tools-2").equivalence_key()
    assert request(side_effect_free=False).equivalence_key() is None
    assert request(authority_scope="").equivalence_key() is None
    assert request(operation_kind="tool").equivalence_key() is None


def test_job_over_limit_is_rejected_fail_closed():
    controller = FairAdmissionController(CapacityLimits(max_runnable=1, max_active_workers=1, max_inference_requests=1, max_backend_slots=1, max_queue=1))
    decision = controller.submit(AdmissionJob("large", "client", "session", memory_bytes=1))
    assert decision.state == "admitted"
    with pytest.raises(ValueError):
        AdmissionJob("invalid", "client", "session", priority="unknown")


def test_equivalent_inference_has_one_executor_and_distinct_receipts():
    async def scenario():
        coordinator = InferenceCoordinator(FairAdmissionController(CapacityLimits(max_runnable=2, max_active_workers=2, max_inference_requests=2, max_backend_slots=2, max_queue=2)))
        calls = 0
        release = asyncio.Event()
        async def execute(_request):
            nonlocal calls
            calls += 1
            await release.wait()
            return {"tokens": ["shared"]}
        first = asyncio.create_task(coordinator.run(request(), execute, correlation_id="first"))
        await asyncio.sleep(0)
        second = asyncio.create_task(coordinator.run(request(), execute, correlation_id="second"))
        await asyncio.sleep(0)
        release.set()
        left, right = await asyncio.gather(first, second)
        assert calls == 1
        assert left.shared_execution_id == right.shared_execution_id
        assert left.correlation_id != right.correlation_id
        assert left.deduplicated is False and right.deduplicated is True
        assert left.result == right.result and left.result is not right.result
    asyncio.run(scenario())


def test_effectful_requests_never_coalesce():
    async def scenario():
        coordinator = InferenceCoordinator()
        calls = 0
        async def execute(_request):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return {"ok": True}
        left, right = await asyncio.gather(coordinator.run(request(side_effect_free=False), execute), coordinator.run(request(side_effect_free=False), execute))
        assert calls == 2
        assert left.equivalence_key is None and right.equivalence_key is None
        assert not left.deduplicated and not right.deduplicated
    asyncio.run(scenario())


def test_one_waiter_cancel_does_not_cancel_shared_work():
    async def scenario():
        coordinator = InferenceCoordinator()
        calls = 0
        release = asyncio.Event()
        async def execute(_request):
            nonlocal calls
            calls += 1
            await release.wait()
            return "done"
        first = asyncio.create_task(coordinator.run(request(), execute, correlation_id="cancelled"))
        await asyncio.sleep(0)
        second = asyncio.create_task(coordinator.run(request(), execute, correlation_id="survivor"))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        receipt = await second
        assert receipt.result == "done"
        assert calls == 1
    asyncio.run(scenario())