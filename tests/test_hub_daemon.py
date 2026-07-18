import json
import tempfile
from pathlib import Path

import pytest

from simplicio_loop.hub_daemon import (
    HubAlreadyRunning,
    HubClient,
    HubDaemon,
    HubEnvelope,
    HubLock,
    HubProtocolError,
)
from simplicio_loop.hub_daemon import HubBackpressureError
from simplicio_loop.hub_scheduler import FairScheduler, ScheduledJob


def test_singleton_lock_and_stale_recovery() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "hub.lock")
        first = HubLock(path)
        second = HubLock(path)
        first.acquire()
        with pytest.raises(HubAlreadyRunning):
            second.acquire()
        first.release()
        Path(path).write_text(json.dumps({"pid": 99999999}), encoding="utf-8")
        second.acquire()
        assert Path(path).exists()
        second.release()


def test_envelope_is_versioned_and_rejects_invalid_messages() -> None:
    envelope = HubEnvelope("req-1", "register", {"client_id": "cli"})
    decoded = HubEnvelope.decode(envelope.encode())
    assert decoded.request_id == "req-1"
    assert decoded.method == "register"
    with pytest.raises(HubProtocolError):
        HubEnvelope.decode('{"schema":"wrong","version":1}')
    with pytest.raises(HubProtocolError):
        HubEnvelope("req-2", "unknown", {}).encode()


def test_daemon_lifecycle_has_no_partial_state_on_invalid_request() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        client = HubClient(daemon, "client-1")
        with pytest.raises(Exception):
            client.request("r0", "register")
        daemon.start()
        assert client.request("r1", "register")["state"] == "registered"
        assert client.request("r2", "submit", job_id="job-1")["job"]["state"] == "queued"
        assert client.request("r3", "claim", job_id="job-1")["job"]["state"] == "claimed"
        assert client.request("r4", "progress", job_id="job-1", progress=50)["job"]["state"] == "running"
        assert client.request("r5", "heartbeat", job_id="job-1")["job"]["state"] == "running"
        assert client.request("r6", "result", job_id="job-1", result={"ok": True})["job"]["state"] == "completed"
        assert client.request("r7", "report", job_id="job-1")["job"]["result"] == {"ok": True}
        with pytest.raises(HubProtocolError):
            client.request("r8", "progress", job_id="job-1", progress=101)
        assert client.request("r9", "report", job_id="job-1")["job"]["state"] == "completed"
        daemon.stop()


def test_restart_clears_session_state_but_not_job_state() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "hub.lock")
        daemon = HubDaemon(path)
        daemon.start()
        client = HubClient(daemon, "client")
        client.request("r1", "register")
        client.request("r2", "submit", job_id="job")
        assert client.request("r3", "cancel", job_id="job")["job"]["state"] == "cancelled"
        daemon.stop()
        restarted = HubDaemon(path)
        restarted.start()
        assert restarted.clients == set()
        report = HubClient(restarted, "client").request("r4", "report", job_id="job")
        assert report["job"]["state"] == "cancelled"
        restarted.stop()


def test_hub_daemon_does_not_lose_job_state_across_restart() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "hub.lock")
        daemon = HubDaemon(path)
        daemon.start()
        client = HubClient(daemon, "client")
        client.request("r1", "register")
        client.request("r2", "submit", job_id="job-durable")
        claimed = client.request("r3", "claim", job_id="job-durable")
        assert claimed["job"]["state"] == "claimed"
        client.request("r4", "progress", job_id="job-durable", progress=42)
        daemon.stop()

        restarted = HubDaemon(path)
        restarted.start()
        report = HubClient(restarted, "client-2").request("r5", "report", job_id="job-durable")
        assert report["job"]["state"] == "running"
        assert report["job"]["progress"] == 42
        assert HubClient(restarted, "client-2").request(
            "r6", "result", job_id="job-durable", result={"ok": True}
        )["job"]["state"] == "completed"
        restarted.stop()

        reopened = HubDaemon(path)
        reopened.start()
        final = HubClient(reopened, "client-3").request("r7", "report", job_id="job-durable")
        assert final["job"]["result"] == {"ok": True}
        reopened.stop()


def test_register_and_ping_are_unaffected_by_durable_job_store() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "hub.lock")
        daemon = HubDaemon(path)
        daemon.start()
        client = HubClient(daemon, "client")
        assert client.request("r1", "register")["state"] == "registered"
        assert daemon.clients == {"client"}
        ping = client.request("r2", "ping")
        assert ping == {"ok": True, "started": True, "clients": 1, "jobs": 0}
        client.request("r3", "submit", job_id="job")
        ping_after_submit = client.request("r4", "ping")
        assert ping_after_submit["jobs"] == 1
        daemon.stop()
        restarted = HubDaemon(path)
        restarted.start()
        assert restarted.clients == set()
        assert restarted.queue.count() == 1
        restarted.stop()


def test_lock_reclaims_corrupt_lock_payload() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "hub.lock")
        Path(path).write_text("not-json-at-all", encoding="utf-8")
        lock = HubLock(path)
        lock.acquire()
        assert json.loads(Path(path).read_text(encoding="utf-8"))["pid"] > 0
        lock.release()
        assert not Path(path).exists()


def test_lock_context_manager_acquires_and_releases() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "hub.lock")
        with HubLock(path) as lock:
            assert Path(path).exists()
            assert lock.path == Path(path)
        assert not Path(path).exists()


def test_envelope_decode_rejects_malformed_and_non_object_payloads() -> None:
    with pytest.raises(HubProtocolError):
        HubEnvelope.decode("{not valid json")
    valid_header = {"schema": "simplicio.hub-ipc/v1", "version": 1}
    with pytest.raises(HubProtocolError):
        HubEnvelope.decode(json.dumps({**valid_header, "method": "not-a-method", "request_id": "r1", "payload": {}}))
    with pytest.raises(HubProtocolError):
        HubEnvelope.decode(json.dumps({**valid_header, "method": "register", "request_id": "", "payload": {}}))
    with pytest.raises(HubProtocolError):
        HubEnvelope.decode(json.dumps({**valid_header, "method": "register", "request_id": "r1", "payload": "nope"}))


def test_handle_register_requires_client_id() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        with pytest.raises(HubProtocolError, match="client_id is required"):
            daemon.handle(HubEnvelope("r1", "register", {}))
        daemon.stop()


def test_handle_execute_rejects_invalid_process_spec_shapes() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        with pytest.raises(HubProtocolError, match="process_spec must be an object"):
            daemon.handle(HubEnvelope("r1", "execute", {"process_spec": "nope"}))
        with pytest.raises(HubProtocolError, match="unknown ProcessSpec fields"):
            daemon.handle(HubEnvelope("r2", "execute", {"process_spec": {"argv": ["true"], "bogus": 1}}))
        with pytest.raises(HubProtocolError, match="unsupported ProcessSpec schema"):
            daemon.handle(HubEnvelope("r3", "execute", {"process_spec": {"argv": ["true"], "schema": "wrong/v9"}}))
        daemon.stop()


def test_handle_requires_job_id_and_rejects_duplicate_submit() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        with pytest.raises(HubProtocolError, match="job_id is required"):
            daemon.handle(HubEnvelope("r1", "claim", {}))
        daemon.handle(HubEnvelope("r2", "submit", {"job_id": "dup", "client_id": "c"}))
        with pytest.raises(HubProtocolError, match="job already exists"):
            daemon.handle(HubEnvelope("r3", "submit", {"job_id": "dup", "client_id": "c"}))
        daemon.stop()


def test_handle_claim_and_heartbeat_reject_wrong_job_state() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        daemon.handle(HubEnvelope("r1", "submit", {"job_id": "job", "client_id": "c"}))
        daemon.handle(HubEnvelope("r2", "claim", {"job_id": "job", "client_id": "c"}))
        with pytest.raises(HubProtocolError, match="job is not claimable"):
            daemon.handle(HubEnvelope("r3", "claim", {"job_id": "job", "client_id": "c"}))
        daemon.handle(HubEnvelope("r4", "cancel", {"job_id": "job", "client_id": "c"}))
        with pytest.raises(HubProtocolError, match="job has no active lease"):
            daemon.handle(HubEnvelope("r5", "heartbeat", {"job_id": "job", "client_id": "c"}))
        daemon.stop()


def test_claim_next_retires_scheduler_entries_with_no_matching_queue_row() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        daemon.scheduler.enqueue(ScheduledJob(task_id="ghost", client_id="c"))
        daemon.handle(HubEnvelope("r1", "submit", {"job_id": "real", "client_id": "c"}))
        result = daemon.handle(HubEnvelope("r2", "claim_next", {"client_id": "worker"}))
        assert result["job"]["job_id"] == "real"
        assert result["job"]["state"] == "claimed"
        daemon.stop()


def test_claim_next_returns_none_when_scheduler_is_empty() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        result = daemon.handle(HubEnvelope("r1", "claim_next", {"client_id": "worker"}))
        assert result == {"ok": True, "job": None}
        daemon.stop()


def test_claim_next_skips_scheduler_entry_whose_queue_row_is_not_queued() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        daemon.handle(HubEnvelope("r1", "submit", {"job_id": "drifted", "client_id": "c"}))
        drifted_task_id = daemon.queue.find_task_id("drifted")
        drifted_payload = dict(daemon.queue.get_row(drifted_task_id)["payload"])
        drifted_payload["state"] = "claimed"
        daemon.queue.update_payload(drifted_task_id, drifted_payload)
        daemon.handle(HubEnvelope("r2", "submit", {"job_id": "ready", "client_id": "c"}))
        result = daemon.handle(HubEnvelope("r3", "claim_next", {"client_id": "worker"}))
        assert result["job"]["job_id"] == "ready"
        daemon.stop()


def test_daemon_start_is_idempotent() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        daemon.start()
        assert daemon.started is True
        daemon.stop()


def test_handle_scheduler_status_reports_real_scheduler_snapshot() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        daemon.handle(HubEnvelope("r1", "submit", {"job_id": "job", "client_id": "c"}))
        result = daemon.handle(HubEnvelope("r2", "scheduler_status", {}))
        assert result["ok"] is True
        assert result["scheduler"]["schema"] == "simplicio.hub-scheduler/v2"
        daemon.stop()


def test_lock_release_is_safe_when_file_already_removed_externally() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "hub.lock")
        lock = HubLock(path)
        lock.acquire()
        Path(path).unlink()
        lock.release()


def test_submit_raises_backpressure_error_when_client_quota_exceeded() -> None:
    with tempfile.TemporaryDirectory() as directory:
        scheduler = FairScheduler(max_queue_per_client=1)
        daemon = HubDaemon(str(Path(directory) / "hub.lock"), scheduler=scheduler)
        daemon.start()
        daemon.handle(HubEnvelope("r1", "submit", {"job_id": "job-1", "client_id": "c"}))
        with pytest.raises(HubBackpressureError) as excinfo:
            daemon.handle(HubEnvelope("r2", "submit", {"job_id": "job-2", "client_id": "c"}))
        assert excinfo.value.signal["scope"] == "client"
        daemon.stop()
