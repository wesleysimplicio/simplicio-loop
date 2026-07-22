"""Fake and real-socket conformance tests for issue #615/#639."""
from __future__ import annotations

import inspect
import json
import tempfile
import sys
import time
from pathlib import Path

import pytest

from simplicio_loop.hub_daemon import HubDaemon, HubSocketClient, HubSocketServer, default_endpoint
from simplicio_loop.hub_queue_agent import (
    CAPABILITY,
    HubQueueAgentClient,
    HubQueueAgentError,
    HubQueueAgentJournal,
    HubQueueAgentUnavailable,
)
from simplicio_loop.stage_agent_coordinator import AdapterRegistry, QueueAgentAdapter


CONTEXT = {
    "run_id": "run-615",
    "task_id": "task-615",
    "attempt_id": "attempt-1",
    "fence": "fence-source",
    "plan_revision": 3,
    "agent_instance_id": "instance-615",
}


class FakeHub:
    def __init__(self):
        self.jobs = {}
        self.calls = []

    def hub_agent_capabilities(self, **payload):
        self.calls.append(("capabilities", payload))
        return {"ok": True, "capabilities": [CAPABILITY]}

    def hub_agent_claim(self, **payload):
        self.calls.append(("claim", payload))
        key = payload["idempotency_key"]
        handle = self.jobs.setdefault(key, {
            "schema": "simplicio.hub-agent-handle/v1", "job_id": "fake-job-1",
            "lease_id": "fake-job-1", "handle_id": "fake-job-1", "generation": 7,
            "fence": "fake-fence", "idempotency_key": key,
        })
        return {"ok": True, "handle": dict(handle)}

    def _job(self, handle):
        handle_id = handle.get("handle_id") if isinstance(handle, dict) else handle
        return next(job for job in self.jobs.values() if job["handle_id"] == handle_id)

    def hub_agent_status(self, **payload):
        self.calls.append(("status", payload))
        job = self._job(payload["handle"])
        return {"ok": True, "status": {"status": job.get("status", "ready"), "heartbeat_at": 1.0}}

    def hub_agent_send(self, **payload):
        self.calls.append(("send", payload))
        job = self._job(payload["handle"])
        job["status"] = "passed"
        return {"ok": True, "state": "passed"}

    def hub_agent_collect(self, **payload):
        self.calls.append(("collect", payload))
        return {"ok": True, "result": {"output": {"fake": True}, "receipt": {"verdict": "pass"}}}

    def hub_agent_cancel(self, **payload):
        self.calls.append(("cancel", payload))
        return {"ok": True, "state": "cancelled"}


def test_fake_hub_preserves_handle_and_journals_lifecycle(tmp_path):
    journal = HubQueueAgentJournal(tmp_path / "agent.jsonl")
    hub = FakeHub()
    client = HubQueueAgentClient(hub, journal=journal)
    adapter = QueueAgentAdapter(queue_client=client)

    handle = client.claim(role="review_panel", stage="validating", context=CONTEXT)
    assert handle["generation"] == 7
    assert client.status(handle)["status"] == "ready"
    client.send(handle, {"payload": {"hello": "hub"}})
    result = client.collect(handle)
    client.cancel(handle, reason="test-cleanup")

    assert result["output"] == {"fake": True}
    assert all(call[1].get("handle") in {"fake-job-1", handle["handle_id"]}
               for call in hub.calls if call[0] in {"status", "send", "collect", "cancel"})
    events = journal.replay()
    assert [event["event_type"] for event in events] == ["intent", "effect", "intent", "effect", "intent", "effect"]
    assert journal.pending() == []

    # QueueAgentAdapter keeps the complete handle, not only its lease id.
    instance = adapter.spawn(role={"role_id": "review_panel"}, stage={"stage_id": "validating"}, stage_context=CONTEXT)
    assert instance.transport_handle is not None
    assert instance.transport_handle["generation"] == 7


def test_strict_client_fails_closed_without_capability():
    class NoHubCapability(FakeHub):
        def hub_agent_capabilities(self, **payload):
            return {"ok": True, "capabilities": []}

    client = HubQueueAgentClient(NoHubCapability())
    assert client.probe() is False
    with pytest.raises(HubQueueAgentUnavailable, match="did not advertise"):
        client.claim(role="role", stage="stage", context=CONTEXT)


def test_hub_registry_strict_does_not_fall_back_to_command():
    client = HubQueueAgentClient(FakeHub())
    queue = QueueAgentAdapter(queue_client=client)
    registry = AdapterRegistry([queue], strict_hub=True)
    assert registry.select(role={"role_id": "role"}, stage={"stage_id": "stage"}) is queue
    with pytest.raises(Exception):
        AdapterRegistry([], strict_hub=True).select(role={"role_id": "role"}, stage={"stage_id": "stage"})


def test_journal_rejects_tampering(tmp_path):
    path = tmp_path / "agent.jsonl"
    journal = HubQueueAgentJournal(path)
    journal.append("intent", {"operation_id": "one"})
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["payload"]["operation_id"] = "tampered"
    path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    with pytest.raises(HubQueueAgentError, match="hash"):
        HubQueueAgentJournal(path)


def _process_spec(*argv: str, timeout: float = 5.0) -> dict:
    return {
        "schema": "simplicio.process-spec/v1", "argv": list(argv), "cwd": str(Path.cwd()),
        "cwd_allowlist": [str(Path.cwd())], "env": {}, "env_allowlist": [],
        "timeout_seconds": timeout, "max_output_bytes": 4096, "priority": 100,
        "idempotency_key": "process-615", "shell": False,
    }


def test_real_hub_socket_lifecycle_stale_fence_and_cancel():
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        endpoint = default_endpoint(directory)
        server = HubSocketServer(daemon, endpoint, "unix")
        server.start()
        try:
            context = dict(CONTEXT, process_spec=_process_spec(sys.executable, "-c", "print('hub-ok')"))
            client = HubQueueAgentClient(HubSocketClient(endpoint, transport="unix"), strict=True)
            handle = client.claim(role="implementation_agent", stage="executing", context=context)
            client.send(handle, context)
            deadline = time.monotonic() + 5
            status = client.status(handle)
            while status["status"] not in {"passed", "failed", "cancelled", "timed_out"} and time.monotonic() < deadline:
                time.sleep(0.01)
                status = client.status(handle)
            assert status["status"] == "passed"
            result = client.collect(handle)
            assert result["process_result"]["returncode"] == 0

            stale = dict(handle, fence=int(handle["fence"]) + 99)
            with pytest.raises(HubQueueAgentError, match="stale fence"):
                client.send(stale, context)

            cancel_context = dict(CONTEXT, attempt_id="attempt-2", process_spec=_process_spec(sys.executable, "-c", "import time; time.sleep(10)"))
            cancel_handle = client.claim(role="review_panel", stage="validating", context=cancel_context)
            client.send(cancel_handle, cancel_context)
            client.cancel(dict(cancel_handle, fence=int(cancel_handle["fence"]) + 1), reason="dependent_failed")
        finally:
            server.shutdown()
            daemon.stop()


def test_real_hub_restart_marks_claimed_recovery_unknown_without_redispatch():
    with tempfile.TemporaryDirectory() as directory:
        lock_path = str(Path(directory) / "hub.lock")
        daemon = HubDaemon(lock_path)
        daemon.start()
        endpoint = default_endpoint(directory)
        server = HubSocketServer(daemon, endpoint, "unix")
        server.start()
        context = dict(CONTEXT, process_spec=_process_spec(sys.executable, "-c", "print('never-dispatched')"))
        client = HubQueueAgentClient(HubSocketClient(endpoint, transport="unix"), strict=True)
        handle = client.claim(role="review_panel", stage="validating", context=context)
        server.shutdown()
        daemon.stop()

        daemon.start()
        restarted_server = HubSocketServer(daemon, endpoint, "unix")
        restarted_server.start()
        try:
            reconnected = HubQueueAgentClient(HubSocketClient(endpoint, transport="unix"), strict=True)
            assert reconnected.recover(handle)["status"] == "recovery_unknown"
            result = reconnected.collect(handle)
            assert result["process_result"] is None
        finally:
            restarted_server.shutdown()
            daemon.stop()


def test_client_architecture_has_no_local_process_or_thread_provider():
    source = inspect.getsource(HubQueueAgentClient)
    assert "subprocess" not in source
    assert "threading" not in source
    assert "supervisor" not in source

def test_client_validation_and_extension_shapes(tmp_path):
    with pytest.raises(HubQueueAgentError, match="invalid JSON"):
        path = tmp_path / "bad.jsonl"
        path.write_text("{", encoding="utf-8")
        HubQueueAgentJournal(path)

    client = HubQueueAgentClient(None)
    with pytest.raises(HubQueueAgentUnavailable):
        client.claim(role="role", stage="stage", context=CONTEXT)
    with pytest.raises(HubQueueAgentError, match="identity"):
        HubQueueAgentClient(FakeHub()).claim(role="role", stage="stage", context={})
    with pytest.raises(HubQueueAgentError, match="handle id"):
        HubQueueAgentClient._handle_id({})


def test_client_rejects_bad_hub_responses_and_conflicts():
    class BadResponse(FakeHub):
        def hub_agent_claim(self, **payload):
            return "not-an-object"
    with pytest.raises(HubQueueAgentError, match="non-object"):
        HubQueueAgentClient(BadResponse()).claim(role="role", stage="stage", context=CONTEXT)

    class Rejected(FakeHub):
        def hub_agent_claim(self, **payload):
            return {"ok": False, "reason_code": "stale_fence", "error": "stale"}
    with pytest.raises(HubQueueAgentError) as raised:
        HubQueueAgentClient(Rejected()).claim(role="role", stage="stage", context=CONTEXT)
    assert raised.value.reason_code == "stale_fence"

    class NoHandle(FakeHub):
        def hub_agent_claim(self, **payload):
            return {"ok": True, "handle": {}}
    with pytest.raises(HubQueueAgentError, match="stable id"):
        HubQueueAgentClient(NoHandle()).claim(role="role", stage="stage", context=CONTEXT)


def test_heartbeat_progress_and_pending_journal(tmp_path):
    class ObservingHub(FakeHub):
        def hub_agent_heartbeat(self, **payload):
            return {"ok": True, "heartbeat_at": 2.0}
        def hub_agent_progress(self, **payload):
            return {"ok": True, "progress": payload["progress"]}
    journal = HubQueueAgentJournal(tmp_path / "journal.jsonl")
    hub = ObservingHub()
    client = HubQueueAgentClient(hub, journal=journal)
    handle = client.claim(role="role", stage="stage", context=CONTEXT)
    assert client.heartbeat(handle)["heartbeat_at"] == 2.0
    assert client.progress(handle, 0.5)["progress"] == 0.5
    journal.append("intent", {"operation_id": "pending"})
    assert journal.pending()[0]["payload"]["operation_id"] == "pending"
