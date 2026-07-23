import asyncio

import pytest

from simplicio_loop.inference_admission import (
    InferenceCoordinator,
    InferenceRequest,
    InferenceRetryExhausted,
    RetryPolicy,
    run_with_retry,
)


def _request():
    return InferenceRequest("model", "backend", "prefix", "request", "tools", "authority", "private", side_effect_free=True)


def test_retry_policy_is_bounded_and_jitter_is_deterministic():
    policy = RetryPolicy(max_attempts=3, base_delay_seconds=0.1, jitter_ratio=0.2)
    assert policy.delay("job", 1) == policy.delay("job", 1)
    assert policy.delay("job", 3) <= policy.base_delay_seconds * (2 ** 2) * (1 + policy.jitter_ratio)


def test_retry_succeeds_after_transient_failures():
    calls = 0

    async def executor(_request):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("transient")
        return {"ok": True}

    receipt = asyncio.run(run_with_retry(InferenceCoordinator(), _request(), executor,
                                         policy=RetryPolicy(base_delay_seconds=0, circuit_failure_threshold=3)))
    assert calls == 3
    assert receipt.result == {"ok": True}


def test_circuit_opens_before_unbounded_retries():
    async def executor(_request):
        raise RuntimeError("backend down")

    with pytest.raises(InferenceRetryExhausted) as caught:
        asyncio.run(run_with_retry(InferenceCoordinator(), _request(), executor,
                                   policy=RetryPolicy(max_attempts=5, base_delay_seconds=0, circuit_failure_threshold=2)))
    assert len(caught.value.decisions) == 2
    assert caught.value.decisions[-1]["reason"] == "circuit_open"
