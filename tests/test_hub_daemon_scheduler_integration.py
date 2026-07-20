import tempfile
from pathlib import Path

import pytest

from simplicio_loop.hub_daemon import HubBackpressureError, HubClient, HubDaemon
from simplicio_loop.hub_scheduler import FairScheduler, ScheduledJob


def test_claim_next_respects_drr_order_across_real_submitted_jobs() -> None:
    """The daemon's IPC claim_next path must actually consult FairScheduler for order,
    not just serve submitted jobs FIFO — heavy submits first and in bulk, light submits
    a few jobs after, yet with equal DRR weight both must interleave rather than heavy
    monopolizing every early claim."""
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        heavy = HubClient(daemon, "heavy")
        light = HubClient(daemon, "light")
        heavy.request("r0", "register")
        light.request("r0", "register")
        for index in range(10):
            heavy.request(f"h{index}", "submit", job_id=f"heavy-{index}")
        for index in range(10):
            light.request(f"l{index}", "submit", job_id=f"light-{index}")

        order = []
        for _ in range(12):
            response = heavy.request("claim", "claim_next")
            assert response["ok"]
            job = response["job"]
            assert job is not None
            order.append(job["client_id"])
            heavy.request("done", "result", job_id=job["job_id"], result={"ok": True})

        assert order[0] == "heavy" and order[1] == "light"
        counts = {"heavy": order.count("heavy"), "light": order.count("light")}
        assert counts["heavy"] == counts["light"] == 6
        daemon.stop()


def test_claim_next_returns_none_when_queue_drained() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        client = HubClient(daemon, "solo")
        client.request("r0", "register")
        client.request("r1", "submit", job_id="job-1")
        response = client.request("r2", "claim_next")
        assert response["job"]["job_id"] == "job-1"
        assert client.request("r3", "claim_next")["job"] is None
        daemon.stop()


def test_cancel_and_result_release_scheduler_quota_slots() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"), scheduler=FairScheduler(max_queue_per_client=1))
        daemon.start()
        client = HubClient(daemon, "a")
        client.request("r0", "register")
        client.request("r1", "submit", job_id="a-1")
        with pytest.raises(Exception):
            client.request("r2", "submit", job_id="a-2")
        client.request("r3", "cancel", job_id="a-1")
        client.request("r4", "submit", job_id="a-3")
        status = daemon.scheduler.status()
        assert status["client_total"]["a"] == 1
        daemon.stop()


def test_submit_over_client_quota_raises_backpressure_with_structured_signal() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"), scheduler=FairScheduler(max_queue_per_client=1))
        daemon.start()
        client = HubClient(daemon, "a")
        client.request("r0", "register")
        client.request("r1", "submit", job_id="a-1")
        with pytest.raises(HubBackpressureError) as excinfo:
            client.request("r2", "submit", job_id="a-2")
        assert excinfo.value.signal["scope"] == "client"
        assert excinfo.value.signal["client_id"] == "a"
        daemon.stop()


def test_submit_with_invalid_weight_raises_scheduler_error_not_backpressure() -> None:
    """A SchedulerError distinct from QuotaExceededError (e.g. a non-positive weight)
    must surface as HubProtocolError, not be mistaken for backpressure."""
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        client = HubClient(daemon, "a")
        client.request("r0", "register")
        with pytest.raises(Exception) as excinfo:
            client.request("r1", "submit", job_id="a-1", weight=0)
        assert not isinstance(excinfo.value, HubBackpressureError)
        daemon.stop()


def test_submit_honors_priority_class_over_ipc() -> None:
    """The daemon's submit payload must reach FairScheduler's priority classes (#505 step
    1), not just weight/cost/workspace_id — an interactive submit must claim ahead of a
    same-weight maintenance submit even though maintenance was queued first."""
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        client = HubClient(daemon, "solo")
        client.request("r0", "register")
        client.request("r1", "submit", job_id="maint-1", client_id="a", priority="maintenance")
        client.request("r2", "submit", job_id="inter-1", client_id="b", priority="interactive")
        response = client.request("r3", "claim_next")
        assert response["job"]["job_id"] == "inter-1"
        daemon.stop()


def test_submit_rejects_unknown_priority_class() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        client = HubClient(daemon, "a")
        client.request("r0", "register")
        with pytest.raises(Exception) as excinfo:
            client.request("r1", "submit", job_id="a-1", priority="urgent")
        assert not isinstance(excinfo.value, HubBackpressureError)
        daemon.stop()


def test_result_after_already_completed_swallows_scheduler_error() -> None:
    """Calling result twice for the same job must not raise even though the second
    scheduler.complete() call hits an already-retired task_id (SchedulerError)."""
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        client = HubClient(daemon, "a")
        client.request("r0", "register")
        client.request("r1", "submit", job_id="a-1")
        client.request("r2", "result", job_id="a-1", result={"ok": True})
        response = client.request("r3", "result", job_id="a-1", result={"ok": True})
        assert response["ok"]
        daemon.stop()


def test_claim_next_retires_stale_scheduler_entry_for_missing_job() -> None:
    """If the scheduler holds an entry for a task_id the durable queue never recorded
    (e.g. injected out-of-band), claim_next must retire it and keep looking rather
    than serve it."""
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        client = HubClient(daemon, "a")
        client.request("r0", "register")
        daemon.scheduler.enqueue(ScheduledJob("ghost-job", "a"))
        client.request("r1", "submit", job_id="a-1")
        response = client.request("r2", "claim_next")
        assert response["job"]["job_id"] == "a-1"
        daemon.stop()


def test_claim_next_retires_non_queued_scheduler_entry() -> None:
    """If a job's payload state was advanced away from 'queued' out from under the
    scheduler (e.g. cancelled without going through scheduler.cancel), claim_next must
    skip that stale entry instead of re-claiming an already-finished job."""
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        client = HubClient(daemon, "a")
        client.request("r0", "register")
        client.request("r1", "submit", job_id="a-1")
        client.request("r2", "submit", job_id="a-2")
        task_id = daemon.queue.find_task_id("a-1")
        row = daemon.queue.get_row(task_id)
        payload = dict(row["payload"])
        payload["state"] = "completed"
        daemon.queue.update_payload(task_id, payload)
        response = client.request("r3", "claim_next")
        assert response["job"]["job_id"] == "a-2"
        daemon.stop()


def test_scheduler_status_ipc_reports_live_state() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        client = HubClient(daemon, "a")
        client.request("r0", "register")
        client.request("r1", "submit", job_id="a-1")
        response = client.request("r2", "scheduler_status")
        assert response["ok"]
        assert response["scheduler"]["global_total"] == 1
        daemon.stop()
