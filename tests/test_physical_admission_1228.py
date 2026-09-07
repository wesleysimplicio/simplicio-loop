from __future__ import annotations

import sys
from types import SimpleNamespace

from simplicio_loop import local_capacity, runner
from simplicio_loop.prism_budgets import BudgetSample
from simplicio_loop.prism_contracts import TaskOwnership
from simplicio_loop.prism_scheduler import (
    AdmissionController,
    PrismPolicy,
    ResourceVector,
    ScheduledTask,
)


def _sample(*, safe_workers: int, unavailable: tuple[str, ...] = (), now_ns: int = 0):
    return local_capacity.CapacitySample(
        requested_workers=2,
        safe_workers=safe_workers,
        cpu_count=8,
        memory_available_bytes=4 << 30 if not unavailable else None,
        disk_free_bytes=10 << 30,
        measured=("cpu_count", "disk_free_bytes") if unavailable else (
            "cpu_count", "disk_free_bytes", "memory_available_bytes",
        ),
        unavailable=unavailable,
        null_reasons=(
            {"memory_available_bytes": "probe_failed", "workers": "required_capacity_signal_unavailable"}
            if unavailable else {}
        ),
        observed_at_ns=now_ns,
    )


def test_physical_monitor_refreshes_on_fake_five_second_clock():
    now = [100]
    seen = []
    samples = [_sample(safe_workers=1, now_ns=100), _sample(safe_workers=1, now_ns=5100)]

    def clock():
        return now[0]

    def probe(_root, *, requested_workers, now_ns, **_kwargs):
        seen.append((requested_workers, now_ns))
        return samples.pop(0)

    monitor = local_capacity.PhysicalAdmissionMonitor(
        ".", 2, sample_interval_ns=5_000, clock=clock, probe=probe,
    )
    first = monitor.refresh()
    assert first.observed_at_ns == 100
    assert monitor.admission(first)["admitted"] is True
    assert len(seen) == 1

    now[0] = 5_099
    assert monitor.refresh() is first
    assert len(seen) == 1
    now[0] = 5_100
    second = monitor.refresh()
    assert second is not first
    assert seen == [(2, 100), (2, 5_100)]


def test_missing_physical_signal_blocks_heavy_admission():
    sample = _sample(safe_workers=0, unavailable=("memory_available_bytes",), now_ns=7)
    admission = local_capacity.PhysicalAdmissionMonitor.admission(sample)
    assert admission["admitted"] is False
    assert admission["reason_code"] == "PHYSICAL_SIGNAL_UNAVAILABLE"

    policy = PrismPolicy(global_worker_limit=2, recovery_reserve=0, validation_reserve=0)
    observation = BudgetSample(
        workers=0, observed_at_ns=7, null_reasons={"workers": "required_capacity_signal_unavailable"},
    ).observation(policy)
    assert observation.limit.workers == 0
    assert "workers" in observation.unavailable
    ownership = TaskOwnership(
        "heavy", "slot", 1, "agent", "lease", 1, "generation", ("implementation",),
        ("accepted", "blocked", "cancelled", "failed", "ready", "running"),
    )
    task = ScheduledTask("heavy", "slot", ownership, resources=ResourceVector(workers=1))
    decision = AdmissionController(policy, observation).decide(task)
    assert decision.reason_code == "PHYSICAL_CAPACITY_UNAVAILABLE"


def test_cgroup_budget_is_a_lower_bound_when_host_memory_is_larger(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        SimpleNamespace(virtual_memory=lambda: SimpleNamespace(available=8 << 30)),
    )
    monkeypatch.setattr(local_capacity, "_cgroup_memory_available", lambda: 1 << 20)
    assert local_capacity._memory_available() == 1 << 20


def test_direct_dispatch_fails_closed_before_worker_submission(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(local_capacity.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(local_capacity, "_memory_available", lambda: None)
    monkeypatch.setattr(local_capacity.shutil, "disk_usage", lambda _path: SimpleNamespace(free=10 << 30))
    monkeypatch.setenv("SIMPLICIO_LOOP_DISPATCH_MODE", "thread")

    def unexpected_worker(_item):
        calls.append(True)
        raise AssertionError("physical admission must gate worker submission")

    monkeypatch.setattr(runner, "_operator_dispatch_attempt", unexpected_worker)
    result = runner.dispatch_operator_batch(
        [{"repo": str(tmp_path), "run_id": "r1", "task_index": 1}],
        max_workers=1,
        retry_budget=0,
    )

    assert calls == []
    assert result["max_workers"] == 0
    assert result["initial_admissions"] == 0
    assert result["capacity_admission"]["reason_code"] == "PHYSICAL_SIGNAL_UNAVAILABLE"
    assert result["drain"]["reason_code"].startswith("PHYSICAL_SIGNAL_UNAVAILABLE:")


def test_pressure_bands_and_monotonic_recovery_window():
    now = [0]
    pressures = iter((90.0, 70.0, 70.0))

    def clock():
        return now[0]

    def probe(_root, *, requested_workers, now_ns, **_kwargs):
        return _sample(safe_workers=1, now_ns=now_ns)

    def pressure_probe(_root):
        return {
            "available": True, "pressure_percent": next(pressures),
            "disk_used_percent": 0.0, "disk_free_bytes": 30 << 30,
        }

    monitor = local_capacity.PhysicalAdmissionMonitor(
        ".", 2, sample_interval_ns=1, clock=clock, probe=probe,
        pressure_probe=pressure_probe,
    )
    monitor.refresh(force=True)
    assert monitor.admission_status()["action"] == "terminate_owned"
    now[0] = 1
    monitor.refresh(force=True)
    assert monitor.admission_status()["reason_code"] == "PHYSICAL_RECOVERY_SUSTAINING"
    now[0] = 60_000_000_001
    monitor.refresh(force=True)
    assert monitor.admission_status()["admitted"] is True


def test_probe_exception_is_converted_to_fail_closed_sample():
    def raising_probe(*_args, **_kwargs):
        raise RuntimeError("probe offline")

    monitor = local_capacity.PhysicalAdmissionMonitor(
        ".", 2, probe=raising_probe,
        pressure_probe=lambda _root: {"available": True, "pressure_percent": 0.0},
    )
    sample = monitor.refresh(force=True)
    admission = monitor.admission_status()
    assert sample.safe_workers == 0
    assert set(sample.unavailable) == {"cpu_count", "disk_free_bytes", "memory_available_bytes"}
    assert admission["reason_code"] == "PHYSICAL_SIGNAL_UNAVAILABLE"
    assert admission["action"] == "suspend_new"
    assert "probe offline" in monitor.status()["probe_error"]


def test_cgroup_cpu_quota_is_respected(monkeypatch):
    class FakePath:
        def __init__(self, value):
            self.value = str(value)

        def read_text(self, encoding="utf-8"):
            if self.value.endswith("cpu.max"):
                return "200000 100000"
            raise OSError("not present")

    monkeypatch.setattr(local_capacity, "Path", FakePath)
    assert local_capacity._cgroup_cpu_capacity() == 2


def test_mission_disk_reserve_is_configurable_and_uses_estimate(monkeypatch):
    monkeypatch.setenv("SIMPLICIO_LOOP_DISK_RESERVE_BYTES", str(20 << 30))
    monitor = local_capacity.PhysicalAdmissionMonitor(
        ".", 2, estimated_disk_bytes=30 << 30,
        probe=lambda *_args, **_kwargs: _sample(safe_workers=1),
        pressure_probe=lambda _root: {"available": True, "pressure_percent": 0.0},
    )
    assert monitor.profile()["disk_reserve_bytes"] == 30 << 30


def test_capacity_block_does_not_allocate_queue_or_leave_pending_receipt(monkeypatch, tmp_path):
    allocated = []

    class Queue:
        def register_tasks(self, _specs):
            pass

        def allocate(self, _spec, **_kwargs):
            allocated.append(True)
            raise AssertionError("allocation must follow physical admission")

    monkeypatch.setattr(local_capacity, "_memory_available", lambda: None)
    monkeypatch.setattr(local_capacity.shutil, "disk_usage", lambda _path: SimpleNamespace(free=30 << 30))
    monkeypatch.setenv("SIMPLICIO_LOOP_DISPATCH_MODE", "thread")
    result = runner.dispatch_operator_batch(
        [{"repo": str(tmp_path), "run_id": "r1", "task_index": 1, "task_id": "t1"}],
        max_workers=1, retry_budget=0, worktree_queue=Queue(),
    )
    assert allocated == []
    assert result["blocked_task_indices"] == [1]
    assert result["workers"][0]["status"] == "blocked"
    assert result["workers"][0]["admission_evidence"]["sample"]["unavailable"]


def test_owned_long_running_child_receives_controlled_pressure_shutdown(monkeypatch, tmp_path):
    import subprocess

    class FakeMonitor:
        def __init__(self, _root, _workers):
            self.sample_interval_ns = 1_000_000
            self.sample = _sample(safe_workers=1, now_ns=1)
            self.refresh_count = 0
            self.due_checks = 0

        def refresh(self, *, force=False):
            self.refresh_count += 1
            self.sample = _sample(safe_workers=1, now_ns=self.refresh_count)
            return self.sample

        def due(self):
            self.due_checks += 1
            return self.due_checks >= 3

        def admission_status(self):
            if self.refresh_count >= 2:
                return {
                    "admitted": False, "reason_code": "PHYSICAL_PRESSURE_TERMINATE",
                    "reason": "pressure_terminate_owned", "action": "terminate_owned",
                    "evidence": {"sample": self.sample.to_dict()},
                }
            return {
                "admitted": True, "reason_code": "PHYSICAL_CAPACITY_AVAILABLE",
                "action": "admit", "evidence": {"sample": self.sample.to_dict()},
            }

        def status(self):
            return {"sample": self.sample.to_dict(), "admission": self.admission_status()}

    child = []
    cancelled = []

    def owned_worker(_item):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
        child.append(proc)
        proc.wait()
        return [{"repo": str(tmp_path), "run_id": "r1", "task_index": 1,
                 "task_id": "owned-child", "worker_id": "w1", "status": "failed",
                 "phase": "blocked", "execution_state": "error", "dead_letter": True,
                 "attempt_count": 1, "dispatch_attempt": 1, "receipt_status": "UNVERIFIED"}]

    def cancel_owned(task_id):
        cancelled.append(task_id)
        if child and child[0].poll() is None:
            child[0].terminate()
            child[0].wait(timeout=2)
        return {"task_id": task_id, "killed": True}

    monkeypatch.setattr(local_capacity, "PhysicalAdmissionMonitor", FakeMonitor)
    monkeypatch.setattr(runner, "_operator_dispatch_attempt", owned_worker)
    monkeypatch.setenv("SIMPLICIO_LOOP_DISPATCH_MODE", "thread")
    result = runner.dispatch_operator_batch(
        [{"repo": str(tmp_path), "run_id": "r1", "task_index": 1, "task_id": "owned-child"}],
        max_workers=1, retry_budget=0, owned_cancel=cancel_owned,
    )
    assert cancelled == ["owned-child"]
    assert result["owned_shutdown"]["status"] == "requested"
    assert result["owned_shutdown"]["cancelled_task_ids"] == ["owned-child"]
    assert child[0].poll() is not None


def test_pressure_bands_and_disk_suspend_profile():
    expected = ((79.0, "admit"), (80.0, "suspend_new"),
                (85.0, "checkpoint_owned_stop"), (88.0, "terminate_owned"))
    for pressure_value, action in expected:
        monitor = local_capacity.PhysicalAdmissionMonitor(
            ".", 2, probe=lambda *_args, **_kwargs: _sample(safe_workers=1),
            pressure_probe=lambda _root, value=pressure_value: {
                "available": True, "pressure_percent": value,
                "disk_used_percent": 0.0, "disk_free_bytes": 30 << 30,
            },
        )
        monitor.refresh(force=True)
        assert monitor.admission_status()["action"] == action

    disk_monitor = local_capacity.PhysicalAdmissionMonitor(
        ".", 2, probe=lambda *_args, **_kwargs: _sample(safe_workers=1),
        pressure_probe=lambda _root: {
            "available": True, "pressure_percent": 40.0,
            "disk_used_percent": 86.0, "disk_free_bytes": (10 << 30) - 1,
        },
    )
    disk_monitor.refresh(force=True)
    disk_admission = disk_monitor.admission_status()
    assert disk_admission["reason_code"] == "PHYSICAL_DISK_SUSPEND"
    assert disk_admission["action"] == "suspend_new"
