import json
import os
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
    HubSocketClient,
    HubSocketServer,
    default_endpoint,
    doctor,
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
        assert daemon.jobs["job-1"]["state"] == "completed"
        daemon.stop()


def test_cancel_and_restart_clear_session_state() -> None:
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
        assert restarted.jobs == {}
        restarted.stop()


def test_real_local_transport_doctor_protocol_and_shutdown() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        lock = root / "hub.lock"
        transport = "named-pipe" if os.name == "nt" else "unix"
        endpoint = default_endpoint(str(root))
        server = HubSocketServer(HubDaemon(str(lock)), endpoint, transport=transport)
        assert doctor(str(lock), endpoint, transport)["reachable"] is False
        server.start()
        assert doctor(str(lock), endpoint, transport)["ok"] is True
        client = HubSocketClient(endpoint, transport)
        assert client.request("p1", "ping")["state"] == "ready"
        assert client.request("r1", "register", client_id="external")["state"] == "registered"
        with pytest.raises(HubProtocolError):
            client.raw_request(json.dumps({"schema": "simplicio.hub-ipc/v1", "version": 99,
                                            "request_id": "bad", "method": "ping", "payload": {}}))
        server.stop()
        server.stop()
        assert doctor(str(lock), endpoint, transport)["reachable"] is False


def test_real_transport_handles_twenty_concurrent_clients() -> None:
    from concurrent.futures import ThreadPoolExecutor

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        transport = "named-pipe" if os.name == "nt" else "unix"
        endpoint = default_endpoint(str(root))
        server = HubSocketServer(HubDaemon(str(root / "hub.lock")), endpoint, transport)
        server.start()
        try:
            def register(index: int) -> str:
                result = HubSocketClient(endpoint, transport).request(
                    "register-%d" % index, "register", client_id="client-%d" % index
                )
                return result["client_id"]

            with ThreadPoolExecutor(max_workers=20) as pool:
                clients = list(pool.map(register, range(20)))
            assert len(set(clients)) == 20
            assert HubSocketClient(endpoint, transport).request("report", "ping")["clients"] == 20
        finally:
            server.stop()
