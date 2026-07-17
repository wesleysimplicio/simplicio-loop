from __future__ import annotations

import pytest

from simplicio_loop.hub_governor import (
    ResourceGovernor,
    ResourceLimits,
    ResourceRequest,
    ResourceThrottled,
)


def test_global_and_client_budgets_are_atomic() -> None:
    governor = ResourceGovernor(
        ResourceLimits(cpu=4, memory_bytes=100),
        client_limits={"alice": ResourceLimits(cpu=2, memory_bytes=80)},
    )
    first = governor.admit("alice", "task-1", ResourceRequest(cpu=2, memory_bytes=50))
    with pytest.raises(ResourceThrottled) as client_error:
        governor.admit("alice", "task-2", ResourceRequest(cpu=1, memory_bytes=1))
    assert client_error.value.receipt["reason"] == "client_budget"
    governor.release(first)
    second = governor.admit("alice", "task-2", ResourceRequest(cpu=2, memory_bytes=50))
    assert second.lease_id == "task-2"
    with pytest.raises(ResourceThrottled) as global_error:
        governor.admit("bob", "task-3", ResourceRequest(cpu=3, memory_bytes=1))
    assert global_error.value.receipt["reason"] == "global_budget"


def test_circuit_breaker_and_recovery() -> None:
    governor = ResourceGovernor(ResourceLimits(cpu=4), circuit_threshold=2, cooldown_seconds=60)
    assert governor.record_failure("oom")["tripped"] is False
    assert governor.record_failure("thrashing")["tripped"] is True
    with pytest.raises(ResourceThrottled) as error:
        governor.admit("alice", "task-1", ResourceRequest(cpu=1))
    assert error.value.receipt["reason"] == "circuit_open"
    governor.recover()
    assert governor.admit("alice", "task-1", ResourceRequest(cpu=1)).task_id == "task-1"


def test_redacted_throttle_receipt() -> None:
    governor = ResourceGovernor(ResourceLimits(processes=1))
    governor.admit("alice", "task-1", ResourceRequest(processes=1), queue="default")
    with pytest.raises(ResourceThrottled) as error:
        governor.admit("alice", "task-2", ResourceRequest(processes=1), queue="default")
    receipt = error.value.receipt
    assert receipt["resource"] == "processes"
    assert receipt["duration_ms"] == 0
    assert "command" not in receipt and "cwd" not in receipt and "env" not in receipt


def test_shutdown_drains_and_releases_leases() -> None:
    governor = ResourceGovernor(ResourceLimits(cpu=2))
    lease = governor.admit("alice", "task-1", ResourceRequest(cpu=1))
    status = governor.shutdown()
    assert status["active_leases"] == 0
    with pytest.raises(ResourceThrottled) as error:
        governor.admit("alice", "task-2", ResourceRequest(cpu=1))
    assert error.value.receipt["reason"] == "draining"
    assert governor.release(lease)["released"] is False


def test_resource_contract_rejects_negative_values() -> None:
    with pytest.raises(ValueError):
        ResourceLimits(cpu=-1)
    with pytest.raises(ValueError):
        ResourceRequest(tokens=-1)


def test_standalone_status_is_observable() -> None:
    governor = ResourceGovernor(ResourceLimits(cpu=2, tokens=100))
    lease = governor.admit("alice", "task-1", ResourceRequest(cpu=1, tokens=20), queue="q1")
    status = governor.status()
    assert status["used"]["cpu"] == 1
    assert status["used"]["tokens"] == 20
    assert status["client_used"]["alice"]["tokens"] == 20
    assert status["active_leases"] == 1
    assert governor.release(lease)["released"] is True
