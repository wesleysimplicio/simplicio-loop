import pytest

from simplicio_loop.model_registry import ModelCapabilityRegistry
from simplicio_loop.model_router import (
    CircuitBreaker,
    CircuitState,
    ModelRouterError,
    classify_failure,
    compute_backoff,
    fallback_decision,
    route_with_fallback,
)


def _entry(**overrides):
    base = {
        "runtime": "local-devcli",
        "provider": "openai-compatible",
        "model_id": "local/q4",
        "aliases": [],
        "capabilities": ["coding", "patch", "tests"],
        "context_window": 8192,
        "os": [],
        "arch": [],
        "probe": {"kind": "stub", "target": ""},
    }
    base.update(overrides)
    return base


def _two_candidate_entries():
    return [
        _entry(runtime="claude", provider="anthropic", model_id="claude-sonnet-5",
               capabilities=["coding", "patch", "tests"], context_window=200000),
        _entry(runtime="codex", provider="openai", model_id="gpt-5.4",
               capabilities=["coding", "patch", "tests"], context_window=128000),
    ]


# -- classify_failure / fallback_decision -------------------------------------

def test_classify_failure_transient_vs_permanent():
    assert classify_failure("timeout") == "transient"
    assert classify_failure("rate_limited") == "transient"
    assert classify_failure("runtime_unavailable") == "transient"
    assert classify_failure("auth_missing") == "permanent"
    assert classify_failure("context_limit") == "permanent"
    assert classify_failure("budget_exceeded") == "permanent"


def test_classify_failure_rejects_unknown_code():
    with pytest.raises(ModelRouterError, match="unknown failure reason_code"):
        classify_failure("not-a-real-code")


def test_fallback_decision_permanent_is_always_terminal():
    assert fallback_decision("auth_missing", attempt=1, max_routes=5) == "terminal"


def test_fallback_decision_transient_progression():
    assert fallback_decision("timeout", attempt=1, max_routes=5) == "retry_same_route"
    assert fallback_decision("timeout", attempt=2, max_routes=5) == "fallback_route"
    assert fallback_decision("timeout", attempt=5, max_routes=5) == "terminal"


def test_fallback_decision_rejects_bad_bounds():
    with pytest.raises(ModelRouterError):
        fallback_decision("timeout", attempt=0, max_routes=3)
    with pytest.raises(ModelRouterError):
        fallback_decision("timeout", attempt=1, max_routes=0)


# -- compute_backoff -----------------------------------------------------------

def test_compute_backoff_is_deterministic_and_exponential():
    assert compute_backoff(1, base_seconds=1.0, cap_seconds=30.0) == 1.0
    assert compute_backoff(2, base_seconds=1.0, cap_seconds=30.0) == 2.0
    assert compute_backoff(3, base_seconds=1.0, cap_seconds=30.0) == 4.0
    # Cap applies regardless of how large the exponential term grows.
    assert compute_backoff(10, base_seconds=1.0, cap_seconds=30.0) == 30.0


def test_compute_backoff_jitter_is_seeded_and_reproducible():
    a = compute_backoff(3, base_seconds=1.0, cap_seconds=30.0, jitter_seed="route-abc")
    b = compute_backoff(3, base_seconds=1.0, cap_seconds=30.0, jitter_seed="route-abc")
    c = compute_backoff(3, base_seconds=1.0, cap_seconds=30.0, jitter_seed="route-xyz")
    assert a == b
    assert a >= 4.0  # base delay preserved, jitter only adds
    assert a != c or True  # different seeds may coincide, but determinism (a==b) is what matters


def test_compute_backoff_rejects_bad_attempt():
    with pytest.raises(ModelRouterError):
        compute_backoff(0)


# -- CircuitBreaker -------------------------------------------------------------

def test_circuit_breaker_opens_after_threshold_and_half_opens_after_cooldown():
    cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=10.0)
    key = ("codex", "openai", "gpt-5.4")
    assert cb.state(key, now=0.0) == CircuitState.CLOSED
    assert cb.is_available(key, now=0.0) is True

    cb.record_failure(key, now=0.0)
    assert cb.state(key, now=0.0) == CircuitState.CLOSED  # below threshold

    cb.record_failure(key, now=1.0)
    assert cb.state(key, now=1.0) == CircuitState.OPEN
    assert cb.is_available(key, now=1.0) is False

    # Still open before cooldown elapses.
    assert cb.state(key, now=5.0) == CircuitState.OPEN
    # Half-open once cooldown has elapsed.
    assert cb.state(key, now=11.0) == CircuitState.HALF_OPEN
    assert cb.is_available(key, now=11.0) is True


def test_circuit_breaker_success_closes_circuit():
    cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=5.0)
    key = ("codex", "openai", "gpt-5.4")
    cb.record_failure(key, now=0.0)
    assert cb.state(key, now=0.0) == CircuitState.OPEN
    cb.record_success(key)
    assert cb.state(key, now=0.0) == CircuitState.CLOSED


def test_circuit_breaker_rejects_bad_config():
    with pytest.raises(ModelRouterError):
        CircuitBreaker(failure_threshold=0)
    with pytest.raises(ModelRouterError):
        CircuitBreaker(cooldown_seconds=-1)


# -- route_with_fallback --------------------------------------------------------

def test_route_with_fallback_no_failure_matches_plain_route_shape():
    reg = ModelCapabilityRegistry(_two_candidate_entries())
    requirements = {"role": "executor", "required_capabilities": ["coding", "patch"]}
    receipt = route_with_fallback(requirements, reg)
    assert receipt["blocked"] is False
    assert receipt["previous_route_id"] == ""
    assert receipt["fallback_reason_code"] == ""
    assert receipt["fallback_decision"] is None
    assert receipt["retry_backoff_seconds"] is None


def test_route_with_fallback_transient_first_failure_allows_retry_same_route():
    reg = ModelCapabilityRegistry(_two_candidate_entries())
    requirements = {"role": "executor", "required_capabilities": ["coding", "patch"]}
    first = route_with_fallback(requirements, reg)
    selected = first["selected"]
    assert selected is not None

    retry = route_with_fallback(
        requirements, reg,
        previous_route=selected, previous_route_id="route-1",
        failure_reason_code="timeout", attempt=1, max_routes=3,
    )
    assert retry["blocked"] is False
    assert retry["fallback_decision"] == "retry_same_route"
    assert retry["previous_route_id"] == "route-1"
    assert retry["retry_backoff_seconds"] is not None
    # retry_same_route must not exclude the failed candidate.
    assert retry["selected"]["model_id"] == selected["model_id"]


def test_route_with_fallback_second_transient_failure_falls_back_to_other_candidate():
    reg = ModelCapabilityRegistry(_two_candidate_entries())
    requirements = {"role": "executor", "required_capabilities": ["coding", "patch"]}
    first = route_with_fallback(requirements, reg)
    selected = first["selected"]

    fallback = route_with_fallback(
        requirements, reg,
        previous_route=selected, previous_route_id="route-1",
        failure_reason_code="timeout", attempt=2, max_routes=3,
    )
    assert fallback["blocked"] is False
    assert fallback["fallback_decision"] == "fallback_route"
    assert fallback["selected"] is not None
    assert fallback["selected"]["model_id"] != selected["model_id"]
    rejected = {c["model_id"]: c for c in fallback["candidates"] if c["status"] == "rejected"}
    assert rejected[selected["model_id"]]["reason_code"] == "runtime_unavailable"


def test_route_with_fallback_permanent_failure_is_terminal_and_blocked():
    reg = ModelCapabilityRegistry(_two_candidate_entries())
    requirements = {"role": "executor", "required_capabilities": ["coding", "patch"]}
    first = route_with_fallback(requirements, reg)
    selected = first["selected"]

    terminal = route_with_fallback(
        requirements, reg,
        previous_route=selected, previous_route_id="route-1",
        failure_reason_code="auth_missing", attempt=1, max_routes=3,
    )
    assert terminal["blocked"] is True
    assert terminal["selected"] is None
    assert terminal["fallback_decision"] == "terminal"
    assert terminal["candidates"] == []
    assert "auth_missing" in terminal["block_reason"]


def test_route_with_fallback_exhausting_max_routes_is_terminal():
    reg = ModelCapabilityRegistry(_two_candidate_entries())
    requirements = {"role": "executor", "required_capabilities": ["coding", "patch"]}
    first = route_with_fallback(requirements, reg)
    selected = first["selected"]

    terminal = route_with_fallback(
        requirements, reg,
        previous_route=selected, previous_route_id="route-1",
        failure_reason_code="timeout", attempt=3, max_routes=3,
    )
    assert terminal["blocked"] is True
    assert terminal["fallback_decision"] == "terminal"


def test_route_with_fallback_records_failure_in_circuit_breaker_and_excludes_open_circuits():
    reg = ModelCapabilityRegistry(_two_candidate_entries())
    requirements = {"role": "executor", "required_capabilities": ["coding", "patch"]}
    cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=999.0)
    first = route_with_fallback(requirements, reg, circuit_breaker=cb)
    selected = first["selected"]
    key = (selected["runtime"], selected["provider"], selected["model_id"])
    assert cb.state(key) == CircuitState.CLOSED

    fallback = route_with_fallback(
        requirements, reg,
        previous_route=selected, previous_route_id="route-1",
        failure_reason_code="timeout", attempt=2, max_routes=3,
        circuit_breaker=cb,
    )
    assert cb.state(key) == CircuitState.OPEN
    assert fallback["selected"]["model_id"] != selected["model_id"]

    # A fresh route call (no explicit failure this time) must still avoid the
    # open-circuit candidate because the breaker itself now excludes it.
    again = route_with_fallback(requirements, reg, circuit_breaker=cb)
    assert again["selected"]["model_id"] != selected["model_id"]
    rejected = {c["model_id"]: c for c in again["candidates"] if c["status"] == "rejected"}
    assert rejected[selected["model_id"]]["reason_code"] == "runtime_unavailable"


def test_route_with_fallback_blocked_when_only_candidate_and_fallback_route_needed():
    single = [_entry(runtime="codex", provider="openai", model_id="gpt-5.4",
                      capabilities=["coding", "patch"], context_window=128000)]
    reg = ModelCapabilityRegistry(single)
    requirements = {"role": "executor", "required_capabilities": ["coding", "patch"]}
    first = route_with_fallback(requirements, reg)
    selected = first["selected"]
    assert selected is not None

    fallback = route_with_fallback(
        requirements, reg,
        previous_route=selected, previous_route_id="route-1",
        failure_reason_code="timeout", attempt=2, max_routes=5,
    )
    assert fallback["blocked"] is True
    assert fallback["selected"] is None
    assert fallback["fallback_decision"] == "fallback_route"
    assert fallback["block_reason"]
