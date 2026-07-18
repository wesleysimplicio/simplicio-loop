from __future__ import annotations

import time

import pytest

from simplicio_loop.hub_governor import (
    PressureReading,
    ResourceGovernor,
    ResourceLimits,
    ResourceProbe,
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


def test_admission_denies_before_any_lease_is_created() -> None:
    governor = ResourceGovernor(ResourceLimits(processes=1))
    governor.admit("alice", "task-1", ResourceRequest(processes=1))
    with pytest.raises(ResourceThrottled):
        governor.admit("bob", "task-2", ResourceRequest(processes=1))
    status = governor.status()
    assert status["active_leases"] == 1
    assert "bob" not in status["client_used"]


def test_probe_falls_back_through_the_ladder_without_crashing(monkeypatch) -> None:
    monkeypatch.setattr(ResourceProbe, "_read_cgroup", staticmethod(lambda: None))
    monkeypatch.setattr(ResourceProbe, "_read_psutil", staticmethod(lambda: None))
    reading = ResourceProbe().read()
    assert reading.source in {"stdlib_fallback", "unavailable"}
    assert reading.memory_bytes >= 0


def test_probe_reports_unavailable_when_every_source_fails(monkeypatch) -> None:
    monkeypatch.setattr(ResourceProbe, "_read_cgroup", staticmethod(lambda: None))
    monkeypatch.setattr(ResourceProbe, "_read_psutil", staticmethod(lambda: None))
    monkeypatch.setattr(ResourceProbe, "_read_stdlib", staticmethod(lambda: None))
    # Disk/gpu are independent of the cpu/memory ladder (disk via stdlib
    # shutil.disk_usage is essentially always available) - patched out here
    # too so this test still proves the fully-unavailable, all-zero case.
    monkeypatch.setattr(ResourceProbe, "_read_disk_percent", staticmethod(lambda path: 0.0))
    monkeypatch.setattr(ResourceProbe, "_read_gpu_percent", staticmethod(lambda: 0.0))
    reading = ResourceProbe().read()
    assert reading == PressureReading(source="unavailable")


def test_probe_measures_disk_percent_independently_of_the_cpu_memory_ladder(monkeypatch) -> None:
    """Disk pressure must still be observable even when cgroups/psutil/stdlib
    all fail to read cpu/memory - it's not gated behind that ladder."""
    monkeypatch.setattr(ResourceProbe, "_read_cgroup", staticmethod(lambda: None))
    monkeypatch.setattr(ResourceProbe, "_read_psutil", staticmethod(lambda: None))
    monkeypatch.setattr(ResourceProbe, "_read_stdlib", staticmethod(lambda: None))
    reading = ResourceProbe().read()
    assert reading.source == "unavailable"
    assert 0.0 <= reading.disk_percent <= 100.0
    assert reading.gpu_percent == 0.0  # no GPU tooling in this environment


def test_probe_gpu_percent_defaults_to_zero_without_nvidia_smi(monkeypatch) -> None:
    import subprocess as subprocess_module

    def _raise(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi not found")

    monkeypatch.setattr(subprocess_module, "run", _raise)
    assert ResourceProbe._read_gpu_percent() == 0.0


def test_evaluate_pressure_trips_on_disk_and_gpu_over_budget() -> None:
    governor = ResourceGovernor(ResourceLimits(cpu=4), circuit_threshold=1, cooldown_seconds=60)
    disk_high = PressureReading(disk_percent=95.0, source="probe")
    disk_result = governor.evaluate_pressure(disk_high, disk_percent_limit=90.0)
    assert disk_result["over_budget"] is True
    assert disk_result["tripped"] is True
    governor.recover()

    gpu_high = PressureReading(gpu_percent=99.0, source="probe")
    gpu_result = governor.evaluate_pressure(gpu_high, gpu_percent_limit=90.0)
    assert gpu_result["over_budget"] is True
    assert gpu_result["tripped"] is True


def test_sustained_pressure_opens_circuit_then_recovers_when_it_subsides() -> None:
    governor = ResourceGovernor(ResourceLimits(cpu=4), circuit_threshold=2, cooldown_seconds=0.01)
    high = PressureReading(memory_bytes=2_000, source="psutil")
    low = PressureReading(memory_bytes=10, source="psutil")

    first = governor.evaluate_pressure(high, memory_limit_bytes=1_000)
    assert first["over_budget"] is True
    assert first["tripped"] is False
    second = governor.evaluate_pressure(high, memory_limit_bytes=1_000)
    assert second["tripped"] is True
    assert second["circuit"]["state"] == "open"

    with pytest.raises(ResourceThrottled) as error:
        governor.admit("alice", "task-1", ResourceRequest(cpu=1))
    assert error.value.receipt["reason"] == "circuit_open"

    time.sleep(0.02)
    recovered = governor.evaluate_pressure(low, memory_limit_bytes=1_000)
    assert recovered["over_budget"] is False
    assert recovered["circuit"]["state"] == "closed"

    lease = governor.admit("alice", "task-1", ResourceRequest(cpu=1))
    assert lease.task_id == "task-1"


def test_standalone_status_is_observable() -> None:
    governor = ResourceGovernor(ResourceLimits(cpu=2, tokens=100))
    lease = governor.admit("alice", "task-1", ResourceRequest(cpu=1, tokens=20), queue="q1")
    status = governor.status()
    assert status["used"]["cpu"] == 1
    assert status["used"]["tokens"] == 20
    assert status["client_used"]["alice"]["tokens"] == 20
    assert status["active_leases"] == 1
    assert governor.release(lease)["released"] is True
