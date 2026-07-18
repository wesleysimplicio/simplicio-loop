import pytest

from simplicio_loop.hub_scheduler import (
    FairScheduler,
    QuotaExceededError,
    ScheduledJob,
    SchedulerError,
)


def jains_fairness_index(values):
    total = sum(values)
    total_sq = sum(v * v for v in values)
    if total_sq == 0:
        return 1.0
    return (total * total) / (len(values) * total_sq)


def test_jains_fairness_index_under_concurrent_heavy_and_light_load() -> None:
    scheduler = FairScheduler(max_inflight_per_client=1000, quantum=1)
    for index in range(300):
        scheduler.enqueue(ScheduledJob(f"heavy-{index}", "heavy"))
    for index in range(300):
        scheduler.enqueue(ScheduledJob(f"light-{index}", "light"))
    served = {"heavy": 0, "light": 0}
    for _ in range(120):
        job = scheduler.next()
        assert job is not None
        served[job.client_id] += 1
        scheduler.complete(job.task_id)
    fairness = jains_fairness_index(list(served.values()))
    assert fairness > 0.95
    assert served["heavy"] > 0 and served["light"] > 0


def test_light_client_not_starved_behind_sustained_heavy_backlog() -> None:
    scheduler = FairScheduler(max_inflight_per_client=1000, quantum=1)
    for index in range(500):
        scheduler.enqueue(ScheduledJob(f"heavy-{index}", "heavy"))
    for index in range(10):
        scheduler.enqueue(ScheduledJob(f"light-{index}", "light"))
    tick = 0
    light_finish_ticks = []
    while scheduler.status()["queued"] > 0:
        job = scheduler.next()
        assert job is not None
        tick += 1
        if job.client_id == "light":
            light_finish_ticks.append(tick)
        scheduler.complete(job.task_id)
        if job.client_id == "light" and len(light_finish_ticks) == 10:
            break
    assert len(light_finish_ticks) == 10
    assert max(light_finish_ticks) <= 22


def test_aging_boosts_deficit_for_long_waiting_client() -> None:
    scheduler = FairScheduler(
        max_inflight_per_client=1000, quantum=1, aging_ticks=3, aging_boost=10
    )
    for index in range(50):
        scheduler.enqueue(ScheduledJob(f"hot-{index}", "hot", weight=5))
    scheduler.enqueue(ScheduledJob("cold-1", "cold", weight=1, cost=5))
    found_cold = False
    for _ in range(60):
        job = scheduler.next()
        if job is None:
            break
        scheduler.complete(job.task_id)
        if job.client_id == "cold":
            found_cold = True
            break
    status = scheduler.status()
    assert found_cold
    assert status["starvation_preventions"] > 0


def test_global_quota_blocks_and_signals_backpressure() -> None:
    scheduler = FairScheduler(max_global_queue=2)
    scheduler.enqueue(ScheduledJob("a-1", "a"))
    scheduler.enqueue(ScheduledJob("b-1", "b"))
    with pytest.raises(QuotaExceededError) as excinfo:
        scheduler.enqueue(ScheduledJob("c-1", "c"))
    signal = excinfo.value.to_backpressure_signal()
    assert signal["scope"] == "global"
    assert signal["limit"] == 2
    job = scheduler.next()
    scheduler.complete(job.task_id)
    scheduler.enqueue(ScheduledJob("c-2", "c"))


def test_workspace_quota_blocks_across_clients_in_same_workspace() -> None:
    scheduler = FairScheduler(max_queue_per_workspace=2)
    scheduler.enqueue(ScheduledJob("a-1", "a", workspace_id="ws1"))
    scheduler.enqueue(ScheduledJob("b-1", "b", workspace_id="ws1"))
    with pytest.raises(QuotaExceededError) as excinfo:
        scheduler.enqueue(ScheduledJob("c-1", "c", workspace_id="ws1"))
    assert excinfo.value.scope == "workspace"
    scheduler.enqueue(ScheduledJob("d-1", "d", workspace_id="ws2"))


def test_client_quota_blocks_over_quota_submission() -> None:
    scheduler = FairScheduler(max_queue_per_client=3, max_inflight_per_client=10)
    for index in range(3):
        scheduler.enqueue(ScheduledJob(f"a-{index}", "a"))
    with pytest.raises(QuotaExceededError) as excinfo:
        scheduler.enqueue(ScheduledJob("a-over", "a"))
    assert excinfo.value.scope == "client"
    assert excinfo.value.client_id == "a"
    scheduler.enqueue(ScheduledJob("b-1", "b"))


def test_cancel_releases_quota_slot() -> None:
    scheduler = FairScheduler(max_queue_per_client=1)
    scheduler.enqueue(ScheduledJob("a-1", "a"))
    with pytest.raises(QuotaExceededError):
        scheduler.enqueue(ScheduledJob("a-2", "a"))
    assert scheduler.cancel("a-1")
    scheduler.enqueue(ScheduledJob("a-3", "a"))


def test_invalid_quota_limits_fail_closed() -> None:
    with pytest.raises(SchedulerError):
        FairScheduler(max_queue_per_client=0)
    with pytest.raises(SchedulerError):
        FairScheduler(aging_ticks=0)
