from pathlib import Path

import pytest

from simplicio_loop.hub_daemon import HubDaemon, HubEnvelope, HubProtocolError
from simplicio_loop.hub_queue_retry import HubRetryQueue
from simplicio_loop.hub_scheduler import (
    FAIR_POLICY_VERSION, LEGACY_POLICY_VERSION, FairScheduler, ScheduledJob,
    SchedulerError, SchedulerPolicy,
)


def test_policy_modes_fail_closed_and_canary_is_deterministic() -> None:
    with pytest.raises(SchedulerError):
        SchedulerPolicy(mode="typo")
    with pytest.raises(SchedulerError):
        SchedulerPolicy(mode="canary", canary_percent=0)
    policy = SchedulerPolicy(mode="canary", canary_percent=25)
    assert policy.policy_for("same") == policy.policy_for("same")
    assert {policy.policy_for(str(i)) for i in range(100)} == {
        FAIR_POLICY_VERSION, LEGACY_POLICY_VERSION
    }


def test_preview_never_dispatches_twice() -> None:
    scheduler = FairScheduler()
    scheduler.enqueue(ScheduledJob("one", "client"))
    assert scheduler.preview_next().task_id == "one"
    assert scheduler.status()["queued"] == 1
    assert scheduler.next().task_id == "one"
    assert scheduler.next() is None


def test_rollout_and_rollback_preserve_existing_job_policy_and_wal(tmp_path: Path) -> None:
    lock = tmp_path / "hub.lock"
    queue_path = tmp_path / "queue.db"
    daemon = HubDaemon(str(lock), str(queue_path))
    daemon.start()
    old = daemon.handle(HubEnvelope("s1", "hub_submit", {
        "payload": {"n": 1}, "idempotency_key": "old", "client_id": "c"
    }))["task_id"]
    receipt = daemon.handle(HubEnvelope("cfg", "scheduler_configure", {
        "mode": "on", "version": "fair-drr-v3", "previous_version": FAIR_POLICY_VERSION
    }))["receipt"]
    assert receipt["dispatch_authorities"] == 1
    new = daemon.handle(HubEnvelope("s2", "hub_submit", {
        "payload": {"n": 2}, "idempotency_key": "new", "client_id": "c"
    }))["task_id"]
    daemon.handle(HubEnvelope("rollback", "scheduler_configure", {
        "mode": "off", "version": "fair-drr-v3", "previous_version": FAIR_POLICY_VERSION
    }))
    daemon.stop()

    queue = HubRetryQueue(str(queue_path))
    rows = {row["task_id"]: row for row in queue.list_queued_scheduling_metadata()}
    assert rows[old]["scheduler_policy"] == FAIR_POLICY_VERSION
    assert rows[new]["scheduler_policy"] == "fair-drr-v3"
    assert queue.scheduler_manifest()["mode"] == "off"
    queue.close()

    restarted = HubDaemon(str(lock), str(queue_path))
    restarted.start()
    try:
        status = restarted.handle(HubEnvelope("status", "scheduler_status", {}))["scheduler"]
        assert status["policy"]["mode"] == "off"
        claimed = [restarted.handle(HubEnvelope(str(i), "hub_claim", {"worker_id": "w", "request": {}}))["claimed"] for i in range(2)]
        assert {item["task_id"] for item in claimed} == {old, new}
    finally:
        restarted.stop()


def test_invalid_rollout_does_not_replace_manifest(tmp_path: Path) -> None:
    daemon = HubDaemon(str(tmp_path / "lock"))
    daemon.start()
    try:
        with pytest.raises(HubProtocolError):
            daemon.handle(HubEnvelope("bad", "scheduler_configure", {
                "mode": "canary", "version": "v2", "previous_version": "v1",
                "canary_percent": 100,
            }))
        assert daemon.queue.scheduler_manifest() is None
    finally:
        daemon.stop()


def test_shadow_compares_without_second_dispatch_authority() -> None:
    scheduler = FairScheduler(policy=SchedulerPolicy(mode="shadow"))
    scheduler.enqueue(ScheduledJob("legacy", "first", cost=8))
    scheduler.enqueue(ScheduledJob("candidate", "second", priority="interactive", cost=8))
    dispatched = scheduler.next()
    assert dispatched.task_id == "legacy"
    status = scheduler.status()
    assert status["queued"] == 1
    assert status["inflight"] == {"first": 1, "second": 0}
    assert status["decision_receipts"][-1] == {
        "schema": "simplicio.hub-scheduler-shadow-receipt/v1",
        "authority": "legacy", "candidate": "candidate", "dispatched": 1,
    }
