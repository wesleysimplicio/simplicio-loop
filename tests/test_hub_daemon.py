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
