from __future__ import annotations

import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from simplicio_loop.hub_governor import (
    GPUReading,
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
    reading = ResourceProbe().read()
    assert reading == PressureReading(source="unavailable")


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


def test_gpu_probe_reads_nvidia_smi_when_binary_present(monkeypatch) -> None:
    class _FakeCompleted:
        returncode = 0
        stdout = "4096, 16384\n"
        stderr = ""

    def _fake_run(*args, **kwargs):
        assert args[0][0] == "nvidia-smi"
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    reading = ResourceProbe().read_gpu()
    assert reading.source == "nvidia-smi"
    assert reading.memory_used_bytes == 4096 * 1024 * 1024
    assert reading.memory_total_bytes == 16384 * 1024 * 1024


def test_gpu_probe_reports_unavailable_when_binary_absent(monkeypatch) -> None:
    def _fake_run(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi not found")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    reading = ResourceProbe().read_gpu()
    assert reading == GPUReading(source="unavailable")


def test_gpu_probe_reports_unavailable_on_nonzero_exit(monkeypatch) -> None:
    class _FakeCompleted:
        returncode = 1
        stdout = ""
        stderr = "no devices found"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted())
    reading = ResourceProbe().read_gpu()
    assert reading.source == "unavailable"


def test_gpu_pressure_trips_and_recovers_circuit() -> None:
    governor = ResourceGovernor(ResourceLimits(cpu=4), circuit_threshold=1, cooldown_seconds=0.01)
    hot = GPUReading(memory_used_bytes=15_000, memory_total_bytes=16_000, source="nvidia-smi")
    cool = GPUReading(memory_used_bytes=100, memory_total_bytes=16_000, source="nvidia-smi")

    result = governor.evaluate_gpu_pressure(hot, memory_used_limit_bytes=10_000)
    assert result["over_budget"] is True
    assert result["tripped"] is True
    with pytest.raises(ResourceThrottled) as error:
        governor.admit("alice", "task-1", ResourceRequest(cpu=1))
    assert error.value.receipt["reason"] == "circuit_open"

    time.sleep(0.02)
    recovered = governor.evaluate_gpu_pressure(cool, memory_used_limit_bytes=10_000)
    assert recovered["over_budget"] is False
    assert recovered["circuit"]["state"] == "closed"
    assert governor.admit("alice", "task-1", ResourceRequest(cpu=1)).task_id == "task-1"


def test_gpu_pressure_unavailable_reading_never_trips() -> None:
    governor = ResourceGovernor(ResourceLimits(cpu=4))
    unavailable = GPUReading(source="unavailable")
    result = governor.evaluate_gpu_pressure(unavailable, memory_used_limit_bytes=1)
    assert result["over_budget"] is False
    assert result["circuit"]["state"] == "closed"


def test_concurrent_admission_never_exceeds_global_or_client_budget() -> None:
    governor = ResourceGovernor(
        ResourceLimits(cpu=6),
        client_limits={f"client-{i}": ResourceLimits(cpu=3) for i in range(4)},
    )
    attempts = 200
    admitted: list = []
    denied = 0
    lock = __import__("threading").Lock()

    def _worker(index: int) -> None:
        nonlocal denied
        client_id = f"client-{index % 4}"
        try:
            lease = governor.admit(client_id, f"task-{index}", ResourceRequest(cpu=1))
        except ResourceThrottled:
            with lock:
                denied += 1
            return
        status = governor.status()
        assert status["used"]["cpu"] <= 6
        assert status["client_used"].get(client_id, {}).get("cpu", 0) <= 3
        time.sleep(0.001)
        governor.release(lease)
        with lock:
            admitted.append(lease.lease_id)

    with ThreadPoolExecutor(max_workers=32) as pool:
        list(pool.map(_worker, range(attempts)))

    assert len(admitted) + denied == attempts
    assert len(admitted) > 0
    final_status = governor.status()
    assert final_status["used"]["cpu"] == 0
    assert final_status["active_leases"] == 0
def test_admit_rejects_missing_client_or_task_id() -> None:
    governor = ResourceGovernor(ResourceLimits(cpu=2))
    with pytest.raises(ValueError):
        governor.admit("", "task-1", ResourceRequest(cpu=1))
    with pytest.raises(ValueError):
        governor.admit("alice", "", ResourceRequest(cpu=1))


def test_admit_is_idempotent_for_the_same_lease_id() -> None:
    governor = ResourceGovernor(ResourceLimits(cpu=2))
    first = governor.admit("alice", "task-1", ResourceRequest(cpu=1), lease_id="lease-a")
    second = governor.admit("alice", "task-1", ResourceRequest(cpu=1), lease_id="lease-a")
    assert first is second
    assert governor.status()["used"]["cpu"] == 1


def test_record_failure_rejects_unsupported_reason() -> None:
    governor = ResourceGovernor(ResourceLimits(cpu=2))
    with pytest.raises(ValueError):
        governor.record_failure("not_a_real_reason")


def test_receipts_returns_a_defensive_copy_of_every_throttle() -> None:
    governor = ResourceGovernor(ResourceLimits(processes=1))
    governor.admit("alice", "task-1", ResourceRequest(processes=1))
    with pytest.raises(ResourceThrottled):
        governor.admit("bob", "task-2", ResourceRequest(processes=1))
    receipts = governor.receipts()
    assert len(receipts) == 1
    assert receipts[0]["reason"] == "global_budget"
    receipts[0]["reason"] = "mutated"
    assert governor.receipts()[0]["reason"] == "global_budget"


def test_standalone_status_is_observable() -> None:
    governor = ResourceGovernor(ResourceLimits(cpu=2, tokens=100))
    lease = governor.admit("alice", "task-1", ResourceRequest(cpu=1, tokens=20), queue="q1")
    status = governor.status()
    assert status["used"]["cpu"] == 1
    assert status["used"]["tokens"] == 20
    assert status["client_used"]["alice"]["tokens"] == 20
    assert status["active_leases"] == 1
    assert governor.release(lease)["released"] is True
