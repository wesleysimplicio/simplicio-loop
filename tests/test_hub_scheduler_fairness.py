import pytest

from simplicio_loop.hub_scheduler import (
    PRIORITY_GAIN_MULTIPLIER,
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


def test_priority_classes_cover_all_seven_from_the_issue() -> None:
    assert set(PRIORITY_GAIN_MULTIPLIER) == {
        "interactive", "mapping", "llm", "test", "build", "background", "maintenance",
    }
    # background is the default and must preserve the pre-priority gain=quantum*weight formula.
    assert PRIORITY_GAIN_MULTIPLIER["background"] == 1.0
    assert PRIORITY_GAIN_MULTIPLIER["interactive"] > PRIORITY_GAIN_MULTIPLIER["maintenance"]


def test_enqueue_rejects_unknown_priority_class() -> None:
    scheduler = FairScheduler()
    with pytest.raises(SchedulerError):
        scheduler.enqueue(ScheduledJob("job-1", "client", priority="urgent"))


def test_higher_priority_job_dispatches_ahead_of_lower_priority_same_weight() -> None:
    scheduler = FairScheduler(max_inflight_per_client=1000, quantum=1)
    scheduler.enqueue(ScheduledJob("maint-1", "a", priority="maintenance"))
    scheduler.enqueue(ScheduledJob("inter-1", "b", priority="interactive"))
    order = []
    for _ in range(2):
        job = scheduler.next()
        assert job is not None
        order.append(job.client_id)
        scheduler.complete(job.task_id)
    assert order[0] == "b"


def test_default_priority_is_background_and_gain_formula_is_unchanged() -> None:
    scheduler = FairScheduler(max_inflight_per_client=1000, quantum=3)
    scheduler.enqueue(ScheduledJob("job-1", "solo", weight=2, cost=5))
    job = scheduler.next()
    assert job is not None
    assert job.priority == "background"
    # gain (3*2*1.0=6) was insufficient for cost=5 on the first tick only if deficit
    # started at 0; here it dispatches once deficit >= cost, matching the prior formula.
    assert scheduler.status()["deficit"]["solo"] == 1


def test_status_reports_native_jains_fairness_index_and_served_total() -> None:
    scheduler = FairScheduler(max_inflight_per_client=1000, quantum=1)
    for index in range(50):
        scheduler.enqueue(ScheduledJob(f"a-{index}", "a"))
        scheduler.enqueue(ScheduledJob(f"b-{index}", "b"))
    for _ in range(60):
        job = scheduler.next()
        assert job is not None
        scheduler.complete(job.task_id)
    status = scheduler.status()
    assert status["served_total"]["a"] == status["served_total"]["b"] == 30
    assert status["jains_fairness_index"] > 0.99


def test_jains_fairness_index_is_perfect_before_any_dispatch() -> None:
    scheduler = FairScheduler()
    scheduler.enqueue(ScheduledJob("job-1", "solo"))
    assert scheduler.status()["jains_fairness_index"] == 1.0


def test_jains_fairness_index_is_perfect_on_an_empty_scheduler() -> None:
    scheduler = FairScheduler()
    assert scheduler.status()["jains_fairness_index"] == 1.0
