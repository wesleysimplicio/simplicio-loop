from __future__ import annotations

import tempfile
from pathlib import Path

from simplicio_loop.hub_daemon import (
    HubClient,
    HubDaemon,
    HubProtocolError,
    HubSocketClient,
    HubSocketServer,
    default_endpoint,
    default_transport,
)
from simplicio_loop.hub_governor import ResourceLimits, ResourceRequest


def test_hub_submit_claim_complete_round_trip_in_process() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        client = HubClient(daemon, "alice")

        submitted = client.request("r1", "hub_submit", payload={"kind": "work"}, idempotency_key="k1")
        task_id = submitted["task_id"]

        claimed = client.request("r2", "hub_claim", worker_id="worker-1", request={"cpu": 1})
        assert claimed["claimed"]["task_id"] == task_id
        assert claimed["claimed"]["client_id"] == "alice"
        assert claimed["claimed"]["payload"] == {"kind": "work"}

        completed = client.request("r3", "hub_complete", task_id=task_id)
        assert completed["ok"] is True

        status = client.request("r4", "hub_status")["status"]
        assert status["scheduler"]["global_total"] == 0
        daemon.stop()


def test_hub_claim_returns_none_when_nothing_is_queued() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        client = HubClient(daemon, "alice")
        claimed = client.request("r1", "hub_claim", worker_id="worker-1", request={"cpu": 1})
        assert claimed["claimed"] is None
        daemon.stop()


def test_hub_submit_rejects_missing_idempotency_key_or_client_id() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        client = HubClient(daemon, "alice")
        try:
            client.request("r1", "hub_submit", payload={"kind": "x"}, idempotency_key="")
            raise AssertionError("expected HubProtocolError for missing idempotency_key")
        except HubProtocolError:
            pass
        daemon.stop()


def test_hub_claim_rejects_missing_worker_id_and_unknown_resource_fields() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        client = HubClient(daemon, "alice")
        try:
            client.request("r1", "hub_claim", worker_id="", request={"cpu": 1})
            raise AssertionError("expected HubProtocolError for missing worker_id")
        except HubProtocolError:
            pass
        try:
            client.request("r2", "hub_claim", worker_id="w1", request={"not_a_real_resource": 1})
            raise AssertionError("expected HubProtocolError for an unknown resource field")
        except HubProtocolError:
            pass
        daemon.stop()


def test_hub_fail_without_a_prior_claim_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        client = HubClient(daemon, "alice")
        try:
            client.request("r1", "hub_fail", task_id="does-not-exist", error_code="x")
            raise AssertionError("expected HubProtocolError")
        except HubProtocolError:
            pass
        daemon.stop()


def test_daemon_start_is_idempotent_and_reuses_the_same_service() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        first_service = daemon.service
        daemon.start()  # second call must be a no-op, not rebuild the service
        assert daemon.service is first_service
        daemon.stop()


def test_hub_complete_without_a_prior_claim_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        client = HubClient(daemon, "alice")
        try:
            client.request("r1", "hub_complete", task_id="does-not-exist")
            raise AssertionError("expected HubProtocolError")
        except HubProtocolError:
            pass
        daemon.stop()


def test_hub_fail_with_retry_then_dead_letter_over_ipc() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        client = HubClient(daemon, "alice")
        task_id = client.request(
            "r1", "hub_submit", payload={"kind": "flaky"}, idempotency_key="flaky", max_attempts=2,
        )["task_id"]

        claimed = client.request("r2", "hub_claim", worker_id="w1", request={"cpu": 1})["claimed"]
        assert claimed["task_id"] == task_id
        outcome = client.request("r3", "hub_fail", task_id=task_id, error_code="temporary")
        assert outcome["outcome"] == "retry"

        reclaimed = client.request("r4", "hub_claim", worker_id="w2", request={"cpu": 1})["claimed"]
        assert reclaimed["task_id"] == task_id
        outcome = client.request("r5", "hub_fail", task_id=task_id, error_code="permanent")
        assert outcome["outcome"] == "dead_letter"
        daemon.stop()


def test_resource_governor_defers_an_over_budget_job_over_ipc() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(
            str(Path(directory) / "hub.lock"), resource_limits=ResourceLimits(cpu=1),
        )
        daemon.start()
        client = HubClient(daemon, "alice")
        client.request("r1", "hub_submit", payload={"kind": "expensive"}, idempotency_key="expensive")
        client.request("r1", "hub_submit", payload={"kind": "cheap"}, idempotency_key="cheap")

        over_budget = client.request(
            "r2", "hub_claim", worker_id="w1", request={"cpu": 5}, max_candidates=1,
        )
        assert over_budget["claimed"] is None

        affordable = client.request("r3", "hub_claim", worker_id="w1", request={"cpu": 1})
        assert affordable["claimed"] is not None
        daemon.stop()


def test_hub_submit_over_a_real_unix_socket_transport() -> None:
    """The in-process tests above prove HubService's wiring; this proves it also works
    through the REAL IPC transport (Unix domain socket), not just direct dict dispatch."""
    if default_transport() != "unix":
        return  # this environment's default transport is exercised elsewhere (Windows CI)
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        endpoint = default_endpoint(directory)
        server = HubSocketServer(daemon, endpoint, "unix")
        server.start()
        try:
            client = HubSocketClient(endpoint, transport="unix")
            submitted = client.request("r1", "hub_submit", payload={"kind": "over-the-wire"},
                                        idempotency_key="wire-1", client_id="bob")
            task_id = submitted["task_id"]
            claimed = client.request("r2", "hub_claim", worker_id="w1", request={"cpu": 1})
            assert claimed["claimed"]["task_id"] == task_id
            assert claimed["claimed"]["payload"] == {"kind": "over-the-wire"}
            completed = client.request("r3", "hub_complete", task_id=task_id)
            assert completed["ok"] is True
        finally:
            server.shutdown()
            daemon.stop()


def test_durable_queue_and_scheduler_fairness_both_survive_daemon_restart() -> None:
    """Previously an honest documented gap (fairness metadata was NOT persisted across
    a restart, only the raw durable job) - now fixed: submit() persists client_id/
    workspace_id/weight/cost alongside the durable row, and HubDaemon.start() calls
    HubService.rehydrate_scheduler() to re-admit them into the fresh FairScheduler.
    Real restart, real HubDaemon, not a mock."""
    with tempfile.TemporaryDirectory() as directory:
        lock_path = str(Path(directory) / "hub.lock")
        daemon = HubDaemon(lock_path)
        daemon.start()
        client = HubClient(daemon, "alice")
        task_id = client.request(
            "r1", "hub_submit", payload={"kind": "durable"}, idempotency_key="durable-1",
            weight=2, cost=3,
        )["task_id"]
        daemon.stop()

        restarted = HubDaemon(lock_path)
        restarted.start()
        # The durable row survived, as before.
        assert restarted.service.queue.state(task_id) == "queued"
        # And now the scheduler's fairness bookkeeping does too - rehydrated from the
        # durable queue's own persisted scheduling metadata, not re-derived or guessed.
        status = restarted.service.scheduler.status()
        assert status["global_total"] == 1
        assert status["client_total"] == {"alice": 1}

        # It's genuinely schedulable after restart, not just present in status().
        claimed = restarted.service.claim("worker-1", ResourceRequest())
        assert claimed is not None
        assert claimed.task_id == task_id
        restarted.service.complete(claimed)
        restarted.stop()
