"""Focused acceptance tests for the continuous pool/resource governor (#150)."""

import threading
import time
import sys
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import fan_out  # noqa: E402


def _capacity(workers=2, cpu=None, memory_mb=None):
    cpu = float(workers if cpu is None else cpu)
    return {
        "workers_local": workers,
        "resources": {
            "cpu": cpu,
            "memory_mb": memory_mb,
            "disk_mb": None,
            "processes": float(workers),
            "quota": None,
        },
    }


def test_pool_refills_slot_without_wave_barrier(tmp_path):
    timeline = {}
    lock = threading.Lock()

    def worker(task, _workdir, _dry_run):
        started = time.monotonic()
        with lock:
            timeline[task.id] = [started, None]
        time.sleep(task.resources.get("sleep", 0.01))
        ended = time.monotonic()
        with lock:
            timeline[task.id][1] = ended
        return fan_out.WorkerResult(task.id, True)

    tasks = [
        fan_out.Task("slow", "slow", files_affected=["slow.py"], resources={"sleep": 0.08}),
        fan_out.Task("fast", "fast", files_affected=["fast.py"], resources={"sleep": 0.02}),
        fan_out.Task("refill", "refill", files_affected=["refill.py"], resources={"sleep": 0.01}),
    ]
    results, scheduler = fan_out.run_scheduler(tasks, str(tmp_path), 2,
                                                capacity=_capacity(2), worker=worker)
    assert all(result.success for result in results)
    # The second slot is refilled as soon as `fast` completes while `slow` is
    # still active; a group/round barrier would start refill after slow.
    assert timeline["refill"][0] < timeline["slow"][1]
    assert any(event.event == "started" and event.task_id == "refill" for event in scheduler.events)


def test_conflict_lane_serializes_only_overlapping_tasks(tmp_path):
    active = set()
    overlaps = []
    timeline = {}
    lock = threading.Lock()

    def worker(task, _workdir, _dry_run):
        with lock:
            if active:
                overlaps.append((task.id, tuple(active)))
            active.add(task.id)
            timeline[task.id] = [time.monotonic(), None]
        time.sleep(0.03)
        with lock:
            active.remove(task.id)
            timeline[task.id][1] = time.monotonic()
        return fan_out.WorkerResult(task.id, True)

    tasks = [
        fan_out.Task("a", "a", files_affected=["shared.py"]),
        fan_out.Task("b", "b", files_affected=["shared.py"]),
        fan_out.Task("c", "c", files_affected=["other.py"]),
    ]
    results, scheduler = fan_out.run_scheduler(tasks, str(tmp_path), 2,
                                                capacity=_capacity(2), worker=worker)
    assert len(results) == 3 and all(result.success for result in results)
    assert not (timeline["a"][0] < timeline["b"][1] and timeline["b"][0] < timeline["a"][1])
    assert timeline["c"][0] < max(timeline["a"][1], timeline["b"][1])
    assert any(event.reason_code == "conflict" for event in scheduler.events
               if event.event in ("idle", "deferred"))


def test_resource_governor_never_exceeds_cpu_cap(tmp_path):
    active = 0
    peak = 0
    lock = threading.Lock()

    def worker(task, _workdir, _dry_run):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return fan_out.WorkerResult(task.id, True)

    tasks = [fan_out.Task(str(i), "cpu", resources={"cpu": 1}) for i in range(8)]
    results, scheduler = fan_out.run_scheduler(tasks, str(tmp_path), 2,
                                                capacity=_capacity(2, cpu=1), worker=worker)
    assert len(results) == len(tasks)
    assert peak == 1
    assert any(event.reason_code == "resource" for event in scheduler.events if event.event == "idle")


def test_retry_requeues_only_failed_worker(tmp_path):
    attempts = {}

    def worker(task, _workdir, _dry_run):
        attempts[task.id] = attempts.get(task.id, 0) + 1
        if attempts[task.id] == 1:
            return fan_out.WorkerResult(task.id, False, error="transient", reason_code="worker_failure")
        return fan_out.WorkerResult(task.id, True)

    task = fan_out.Task("retry", "retry", retries=1)
    results, scheduler = fan_out.run_scheduler([task], str(tmp_path), 1,
                                                capacity=_capacity(1), worker=worker)
    assert len(results) == 1 and results[0].success
    assert results[0].attempts == 2
    assert any(event.event == "requeued" and event.reason_code == "retry" for event in scheduler.events)


def test_zero_capacity_is_blocked_without_fake_receipts(tmp_path):
    task = fan_out.Task("blocked", "blocked")
    results, scheduler = fan_out.run_scheduler([task], str(tmp_path), 2,
                                                capacity=_capacity(0, cpu=0))
    assert results == []
    assert any(event.event == "blocked" and event.reason_code == "resource" for event in scheduler.events)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_fan_out_scheduler")
