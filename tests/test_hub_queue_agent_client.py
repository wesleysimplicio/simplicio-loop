"""Conformance and system coverage for the issue #615 Hub stage-agent provider."""
from __future__ import annotations

import ast
import json
import os
import sys
import time
from pathlib import Path

import pytest

from simplicio_loop import stage_agent_coordinator as sc
from simplicio_loop.hub_daemon import HubDaemon, HubSocketServer


ROOT = Path(__file__).resolve().parent.parent
ECHO = ROOT / "contracts/stage-agents/v1/adapter-fixtures/echo_agent.py"


def _context(**updates):
    value = {
        "role_id": "review_panel", "stage_id": "validating", "run_id": "run-615",
        "task_id": "task-615", "attempt_id": "attempt-1", "fence": "fence-1",
        "plan_revision": 1, "timeout_seconds": 10,
    }
    value.update(updates)
    return value


class RecordingClient:
    def __init__(self, *, execute_result=None, unavailable=False):
        self.calls = []
        self.execute_result = execute_result or {
            "returncode": 0, "stdout": "ok", "stderr": "", "duration_seconds": 0.012,
            "timed_out": False, "cancelled": False, "truncated": False,
            "error_code": "", "lease_id": "lease",
        }
        self.unavailable = unavailable

    def request(self, request_id, method, **payload):
        self.calls.append((method, payload))
        if self.unavailable:
            raise OSError("hub offline")
        if method == "ping":
            return {"ok": True, "started": True}
        if method == "execute":
            return {"ok": True, "result": dict(self.execute_result), "lease_id": payload["process_spec"]["idempotency_key"]}
        if method in {"submit", "claim", "heartbeat", "progress", "result", "cancel"}:
            return {"ok": True, "job": {"state": "running"}}
        return {"ok": True}


def _client(tmp_path, recorder, **kwargs):
    return sc.HubQueueAgentClient(
        command=[sys.executable, str(ECHO), "{input}", "{output}", "{receipt}"],
        client_factory=lambda: recorder, journal_path=tmp_path / "journal.jsonl",
        base_tmp_dir=tmp_path / "runs", cwd=ROOT, **kwargs,
    )


def test_process_spec_is_safe_lossless_and_metadata_is_propagated(tmp_path):
    recorder = RecordingClient()
    client = _client(tmp_path, recorder, resources={"weight": 2, "cost": 3})
    claim = client.claim(role="review_panel", stage="validating", context=_context())
    run = client._runs[claim["lease_id"]]
    spec = run.process_spec.to_dict()
    assert spec["shell"] is False
    assert Path(spec["cwd"]).is_absolute()
    assert spec["cwd_allowlist"] == [str(ROOT)]
    assert spec["idempotency_key"] == claim["lease_id"]
    assert spec["priority"] == 100
    submit = next(payload for method, payload in recorder.calls if method == "submit")
    assert submit["priority"] == "test"
    assert (submit["weight"], submit["cost"]) == (2, 3)
    assert submit["metadata"]["run_id"] == "run-615"
    assert submit["metadata"]["process_id"] == claim["lease_id"]


def test_provider_rejects_empty_command_and_non_allowlisted_environment(tmp_path):
    with pytest.raises(sc.StageCoordinatorError) as raised:
        sc.HubQueueAgentClient(command=[], journal_path=tmp_path / "j.jsonl")
    assert raised.value.reason_code == "invalid_command"
    with pytest.raises(sc.StageCoordinatorError) as raised:
        _client(tmp_path, RecordingClient(), extra_env={"SECRET": "not-allowed"})
    assert raised.value.reason_code == "unsafe_environment"


def test_fake_hub_execute_heartbeat_result_and_cancel(tmp_path):
    recorder = RecordingClient()
    client = _client(tmp_path, recorder)
    claim = client.claim(role="review_panel", stage="validating", context=_context())
    lease = claim["lease_id"]
    client.send(lease, _context())
    deadline = time.monotonic() + 3
    while client.status(lease)["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert client.status(lease)["status"] == "passed"
    assert {method for method, _ in recorder.calls} >= {"heartbeat", "progress", "execute", "result"}
    result = client.collect(lease)
    assert result["process_result"]["duration_seconds"] == 0.012
    assert result["process_result"]["truncated"] is False
    client.cancel(lease, reason="dependent failed")
    assert any(method == "cancel" for method, _ in recorder.calls)


@pytest.mark.parametrize(
    ("process_result", "status"),
    [
        ({"returncode": None, "timed_out": True, "cancelled": False, "truncated": True}, "timed_out"),
        ({"returncode": -9, "timed_out": False, "cancelled": True, "truncated": False}, "cancelled"),
        ({"returncode": 137, "timed_out": False, "cancelled": False, "truncated": True, "error_code": "oom"}, "failed"),
    ],
)
def test_timeout_dead_process_limited_output_and_oom_are_not_success(tmp_path, process_result, status):
    recorder = RecordingClient(execute_result=process_result)
    client = _client(tmp_path, recorder)
    lease = client.claim(role="review_panel", stage="validating", context=_context())["lease_id"]
    client.send(lease, _context())
    deadline = time.monotonic() + 3
    while client.status(lease)["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert client.status(lease)["status"] == status
    assert client.collect(lease)["process_result"].get("returncode") == process_result["returncode"]


def test_restart_replays_completed_execute_without_duplicate_effect(tmp_path):
    first_hub = RecordingClient()
    first = _client(tmp_path, first_hub)
    lease = first.claim(role="review_panel", stage="validating", context=_context())["lease_id"]
    first.send(lease, _context())
    deadline = time.monotonic() + 3
    while first.status(lease)["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert sum(method == "execute" for method, _ in first_hub.calls) == 1

    restarted_hub = RecordingClient()
    resumed = _client(tmp_path, restarted_hub)
    replayed = resumed.claim(role="review_panel", stage="validating", context=_context())
    resumed.send(replayed["lease_id"], _context())
    assert replayed["replayed"] is True
    assert not any(method == "execute" for method, _ in restarted_hub.calls)


def test_stale_fence_is_rejected_before_execute(tmp_path):
    recorder = RecordingClient()
    client = _client(tmp_path, recorder)
    lease = client.claim(role="review_panel", stage="validating", context=_context())["lease_id"]
    with pytest.raises(sc.StageCoordinatorError, match="stale Hub lease fence"):
        client.send(lease, _context(fence="fence-stale"))
    assert not any(method == "execute" for method, _ in recorder.calls)


def test_strict_mode_fails_closed_and_never_selects_command(tmp_path):
    command = sc.CommandAgentAdapter(command=[sys.executable, str(ECHO)])
    registry = sc.AdapterRegistry([command], strict_hub=True)
    with pytest.raises(sc.StageCoordinatorError) as raised:
        registry.select(role={"role_id": "review_panel"}, stage={"stage_id": "validating"})
    assert raised.value.reason_code == "hub_required"

    offline = _client(tmp_path, RecordingClient(unavailable=True))
    registry = sc.AdapterRegistry([sc.QueueAgentAdapter(queue_client=offline), command], strict_hub=True)
    with pytest.raises(sc.StageCoordinatorError) as raised:
        registry.select(role={"role_id": "review_panel"}, stage={"stage_id": "validating"})
    assert raised.value.reason_code == "hub_unavailable"


def test_provider_architecture_contains_no_subprocess_calls():
    source = Path(sc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    provider = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "HubQueueAgentClient")
    calls = [
        node for node in ast.walk(provider)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"
    ]
    assert calls == []


@pytest.mark.skipif(os.name == "nt", reason="Unix Hub socket system lane")
def test_real_hub_socket_executes_full_coordinator_stage(tmp_path, monkeypatch):
    from simplicio_loop import process_supervisor_rust

    monkeypatch.setattr(process_supervisor_rust, "rust_binary_path", lambda: None)
    endpoint = str(tmp_path / "hub.sock")
    daemon = HubDaemon(str(tmp_path / "hub.lock"))
    daemon.start()
    server = HubSocketServer(daemon, endpoint)
    server.start()
    try:
        hub = sc.HubQueueAgentClient(
            command=[sys.executable, str(ECHO), "{input}", "{output}", "{receipt}"],
            endpoint=endpoint, transport="unix", journal_path=tmp_path / "journal.jsonl",
            base_tmp_dir=tmp_path / "runs", cwd=ROOT,
        )
        coordinator = sc.StageAgentCoordinator(
            run_id="run-real-615", task_id="task-real-615",
            adapters=[sc.QueueAgentAdapter(queue_client=hub)], strict_hub=True,
            journal=sc.StageCoordinatorJournal(tmp_path / "coordinator.jsonl"),
            poll_interval_seconds=0.005,
        )
        outcome = coordinator.run_stage("intake", fence="fence-real", deadline_seconds=15)
        assert outcome.status == "passed"
        assert outcome.instance.adapter_kind == "queue"
        assert outcome.instance.output["process_result"]["returncode"] == 0
        events = [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text().splitlines()]
        assert any(event["phase"] == "before" and event["effect"] == "execute" for event in events)
        assert any(event["phase"] == "after" and event["effect"] == "execute" for event in events)
    finally:
        server.shutdown()
        daemon.stop()
