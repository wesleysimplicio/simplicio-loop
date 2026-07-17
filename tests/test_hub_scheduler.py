import pytest

from simplicio_loop.hub_scheduler import FairScheduler, ScheduledJob, SchedulerError


def test_drr_fairness_and_no_starvation() -> None:
    scheduler = FairScheduler(max_inflight_per_client=2, quantum=1)
    for client in ("a", "b", "c"):
        for index in range(10):
            scheduler.enqueue(ScheduledJob(f"{client}-{index}", client))
    counts = {"a": 0, "b": 0, "c": 0}
    claimed = []
    for _ in range(12):
        job = scheduler.next()
        assert job is not None
        claimed.append(job)
        counts[job.client_id] += 1
        scheduler.complete(job.task_id)
    assert all(value > 0 for value in counts.values())
    assert scheduler.status()["queued"] == 18


def test_client_quota_and_cancel_are_enforced() -> None:
    scheduler = FairScheduler(max_inflight_per_client=1)
    scheduler.enqueue(ScheduledJob("a-1", "a"))
    scheduler.enqueue(ScheduledJob("a-2", "a"))
    first = scheduler.next()
    assert first is not None
    assert scheduler.next() is None
    assert scheduler.cancel("a-2")
    scheduler.complete(first.task_id)
    assert scheduler.next() is None


def test_invalid_and_duplicate_jobs_fail_closed() -> None:
    scheduler = FairScheduler()
    with pytest.raises(SchedulerError):
        scheduler.enqueue(ScheduledJob("", "client"))
    scheduler.enqueue(ScheduledJob("one", "client"))
    with pytest.raises(SchedulerError):
        scheduler.enqueue(ScheduledJob("one", "client"))
    with pytest.raises(SchedulerError):
        scheduler.complete("missing")
