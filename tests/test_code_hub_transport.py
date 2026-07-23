"""External-process proof for Code's Loop Hub client wire contract."""

import json
import os
import socket
import tempfile
from pathlib import Path

import pytest

from simplicio_loop.hub_daemon import HubDaemon, HubSocketServer, CODE_HUB_CLIENT_SCHEMA, CODE_HUB_PROTOCOL
from simplicio_loop.runtime_bridge import RuntimeBridge, RuntimeBridgeError


def _request(stream, reader, request_id, method, payload):
    value = _request_raw(stream, reader, request_id, method, payload)
    assert value["schema"] == CODE_HUB_CLIENT_SCHEMA
    assert value["id"] == request_id
    assert value["ok"] is True, value
    return value["result"]


def _request_raw(stream, reader, request_id, method, payload):
    stream.sendall((json.dumps({"schema": CODE_HUB_CLIENT_SCHEMA, "id": request_id,
                                "method": method, "payload": payload}) + "\n").encode())
    line = reader.readline()
    return json.loads(line)


def test_code_client_contract_uses_one_hub_identity_and_replays_lifecycle():
    with tempfile.TemporaryDirectory() as directory:
        lock = str(Path(directory) / "hub.lock")
        endpoint = str(Path(directory) / "hub.sock")
        daemon = HubDaemon(lock)
        daemon.start()
        server = HubSocketServer(daemon, endpoint, transport="unix")
        server.start()
        try:
            client, workspace, session = "code", "workspace", "session"
            stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stream.connect(endpoint)
            reader = stream.makefile("rb")
            handshake = _request(stream, reader, 1, "handshake", {
                "schema": CODE_HUB_CLIENT_SCHEMA, "protocol": CODE_HUB_PROTOCOL,
                "client_id": client, "workspace_id": workspace, "session_id": session,
            })
            assert handshake["hub_id"].startswith("loop-hub:")
            assert {item["name"] for item in handshake["services"]} == {"runtime", "mapper", "scheduler", "inference"}
            assert all(item["owner"] == "loop-hub" for item in handshake["services"])
            attached = _request(stream, reader, 2, "attach", {
                "schema": CODE_HUB_CLIENT_SCHEMA, "protocol": CODE_HUB_PROTOCOL,
                "client_id": client, "workspace_id": workspace, "session_id": session,
                "reconnect": False, "cursors": [],
            })
            assert attached["accepted"] is True
            submitted = _request(stream, reader, 3, "submit", {
                "schema": CODE_HUB_CLIENT_SCHEMA, "session_id": session,
                "goal_id": "goal", "turn_id": "turn", "idempotency_key": "turn-key",
                "priority": "interactive", "payload": {},
            })
            assert submitted["workflow_id"] == "turn-key"
            progress = _request(stream, reader, 4, "progress", {
                "workflow_id": "turn-key", "after_sequence": 0,
            })
            assert progress["workflow_id"] == "turn-key"
            cancelled = _request(stream, reader, 5, "cancel", {
                "workflow_id": "turn-key", "session_id": session,
                "idempotency_key": "cancel-key", "reason": "test",
            })
            assert cancelled["state"] == "cancelled"
            reader.close()
        finally:
            stream.close()
            server.shutdown()
            daemon.stop()


def test_code_runtime_execute_is_forwarded_to_hub_owned_runtime_bridge():
    class RecordingRuntime:
        def __init__(self):
            self.calls = []

        def execute(self, workspace, argv, **kwargs):
            self.calls.append((workspace, argv, kwargs))
            return {
                "schema": "simplicio.exec-result/v1",
                "stdout": "hub-runtime-ok",
                "stderr": "",
                "exit_code": 0,
                "effect_state": "completed",
            }

        def close(self):
            pass

    with tempfile.TemporaryDirectory() as directory:
        lock = str(Path(directory) / "hub.lock")
        endpoint = str(Path(directory) / "hub.sock")
        bridge = RecordingRuntime()
        daemon = HubDaemon(lock, runtime_bridge=bridge)
        daemon.start()
        server = HubSocketServer(daemon, endpoint, transport="unix")
        server.start()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.connect(endpoint)
                reader = stream.makefile("rb")
                result = _request(stream, reader, 1, "runtime_execute", {
                    "workspace": directory,
                    "cwd": ".",
                    "argv": ["printf", "hub-runtime-ok"],
                    "env": {},
                    "timeout_ms": 1000,
                    "max_output_bytes": 1024,
                    "idempotency_key": "runtime-idem-1",
                })
                assert result["schema"] == "simplicio.loop-runtime-execution/v1"
                assert result["result"]["effect_state"] == "completed"
                assert bridge.calls[0][0] == directory
                assert bridge.calls[0][1] == ["printf", "hub-runtime-ok"]
                assert bridge.calls[0][2]["idempotency_key"] == "runtime-idem-1"
                reader.close()
        finally:
            server.shutdown()
            daemon.stop()


def test_code_runtime_call_is_forwarded_to_hub_owned_runtime_bridge():
    class RecordingRuntime:
        def __init__(self):
            self.calls = []

        def runtime_call(self, workspace, tool, arguments, **kwargs):
            self.calls.append((workspace, tool, arguments, kwargs))
            return {
                "schema": "simplicio.fs-stat-result/v1",
                "path": arguments["path"],
                "type": "file",
                "size": 12,
            }

        def close(self):
            pass

    with tempfile.TemporaryDirectory() as directory:
        lock = str(Path(directory) / "hub.lock")
        endpoint = str(Path(directory) / "hub.sock")
        bridge = RecordingRuntime()
        daemon = HubDaemon(lock, runtime_bridge=bridge)
        daemon.start()
        server = HubSocketServer(daemon, endpoint, transport="unix")
        server.start()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.connect(endpoint)
                reader = stream.makefile("rb")
                result = _request(stream, reader, 1, "runtime_call", {
                    "workspace": directory,
                    "tool": "simplicio_fs_stat",
                    "arguments": {"repo": directory, "path": "probe.txt"},
                    "timeout_ms": 1000,
                    "idempotency_key": "runtime-call-idem-1",
                })
                assert result["schema"] == "simplicio.loop-runtime-call/v1"
                assert result["tool"] == "simplicio_fs_stat"
                assert result["result"]["schema"] == "simplicio.fs-stat-result/v1"
                assert bridge.calls[0][1] == "simplicio_fs_stat"
                assert bridge.calls[0][2]["repo"] == directory
                assert bridge.calls[0][3]["idempotency_key"] == "runtime-call-idem-1"
                assert "__runtime_effect_transaction" not in bridge.calls[0][2]
                reader.close()
        finally:
            server.shutdown()
            daemon.stop()


def test_runtime_bridge_fake_process_injects_transaction_and_reuses_process(monkeypatch):
    class FakeProcess:
        instances = []

        def __init__(self, binary, workspace):
            self.binary = binary
            self.workspace = workspace
            self.process = self
            self.calls = []
            self.closed = False
            self.__class__.instances.append(self)

        def poll(self):
            return None

        def call_tool(self, name, arguments, *, timeout=10.0):
            self.calls.append((name, arguments))
            return {"schema": "simplicio.fs-stat-result/v1", "exists": True}

        def close(self):
            self.closed = True

    monkeypatch.setattr("simplicio_loop.runtime_bridge._RuntimeProcess", FakeProcess)
    with tempfile.TemporaryDirectory() as directory:
        bridge = RuntimeBridge(binary="fake-runtime")
        first = bridge.runtime_call(
            directory,
            "simplicio_fs_stat",
            {"repo": directory, "path": "probe.txt"},
            idempotency_key="fake-runtime-call-1",
        )
        second = bridge.runtime_call(
            directory,
            "simplicio_fs_stat",
            {"repo": directory, "path": "probe.txt"},
            idempotency_key="fake-runtime-call-2",
        )
        assert first["schema"] == "simplicio.fs-stat-result/v1"
        assert second["exists"] is True
        assert len(FakeProcess.instances) == 1
        process = FakeProcess.instances[0]
        assert [call[0] for call in process.calls] == ["simplicio_fs_stat", "simplicio_fs_stat"]
        transaction = process.calls[0][1]["__runtime_effect_transaction"]
        assert transaction["schema"] == "simplicio.effect-transaction/v1"
        assert transaction["request"]["capability"] == "simplicio_fs_stat"
        assert transaction["request"]["idempotency_key"] == "fake-runtime-call-1"
        assert process.calls[0][1]["repo"] == directory


def test_runtime_call_rejects_unsafe_tool_and_caller_transaction():
    bridge = RuntimeBridge(binary="not-started-by-validation")
    with tempfile.TemporaryDirectory() as directory:
        with pytest.raises(RuntimeBridgeError, match="safe simplicio_"):
            bridge.runtime_call(directory, "python", {}, idempotency_key="unsafe-1")
        with pytest.raises(RuntimeBridgeError, match="bridge-owned"):
            bridge.runtime_call(
                directory,
                "simplicio_fs_stat",
                {"__runtime_effect_transaction": {}},
                idempotency_key="override-1",
            )


@pytest.mark.external_integration
def test_code_runtime_execute_reaches_real_runtime_mcp():
    binary = os.environ.get("SIMPLICIO_RUNTIME_BIN")
    if not binary:
        pytest.skip("set SIMPLICIO_RUNTIME_BIN to a compatible Runtime binary")
    with tempfile.TemporaryDirectory() as directory:
        lock = str(Path(directory) / "hub.lock")
        endpoint = str(Path(directory) / "hub.sock")
        daemon = HubDaemon(lock, runtime_bridge=RuntimeBridge(binary=binary))
        daemon.start()
        server = HubSocketServer(daemon, endpoint, transport="unix")
        server.start()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.connect(endpoint)
                reader = stream.makefile("rb")
                result = _request(stream, reader, 1, "runtime_execute", {
                    "workspace": str(Path.cwd()),
                    "cwd": ".",
                    "argv": ["printf", "real-runtime-ok"],
                    "env": {},
                    "timeout_ms": 2000,
                    "max_output_bytes": 1024,
                    "idempotency_key": "real-runtime-e2e-1",
                })
                assert result["result"]["schema"] == "simplicio.exec-receipt/v1"
                assert result["result"]["stdout"]["data"] == "real-runtime-ok"
                assert result["result"]["success"] is True
                reader.close()
        finally:
            server.shutdown()
            daemon.stop()


@pytest.mark.external_integration
def test_code_runtime_call_reaches_real_runtime_fs_stat():
    binary = os.environ.get("SIMPLICIO_RUNTIME_BIN")
    if not binary:
        pytest.skip("set SIMPLICIO_RUNTIME_BIN to a compatible Runtime binary")
    with tempfile.TemporaryDirectory() as directory:
        probe = Path(directory) / "probe.txt"
        probe.write_text("runtime-stat-probe", encoding="utf-8")
        lock = str(Path(directory) / "hub.lock")
        endpoint = str(Path(directory) / "hub.sock")
        daemon = HubDaemon(lock, runtime_bridge=RuntimeBridge(binary=binary))
        daemon.start()
        server = HubSocketServer(daemon, endpoint, transport="unix")
        server.start()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.connect(endpoint)
                reader = stream.makefile("rb")
                try:
                    value = _request_raw(stream, reader, 1, "runtime_call", {
                        "workspace": directory,
                        "tool": "simplicio_fs_stat",
                        "arguments": {"repo": directory, "path": "probe.txt"},
                        "timeout_ms": 2000,
                        "idempotency_key": "real-runtime-stat-1",
                    })
                finally:
                    reader.close()
                if value.get("ok") is False and "unknown tool" in str(value.get("error", "")):
                    pytest.skip("installed Runtime does not expose simplicio_fs_stat")
                assert value["ok"] is True, value
                stat = value["result"]["result"]
                result = value["result"]
                assert result["schema"] == "simplicio.loop-runtime-call/v1"
                assert stat["schema"] == "simplicio.fs-stat-result/v1"
                assert stat["exists"] is True, stat
                assert stat["type"] == "file", stat
                assert stat["size"] == len("runtime-stat-probe"), stat
                assert len(daemon.runtime_bridge._processes) == 1
        finally:
            server.shutdown()
            daemon.stop()


def test_external_worker_contract_is_durable_idempotent_and_cancel_fail_closed():
    with tempfile.TemporaryDirectory() as directory:
        lock = str(Path(directory) / "hub.lock")
        endpoint = str(Path(directory) / "hub.sock")
        daemon = HubDaemon(lock)
        daemon.start()
        server = HubSocketServer(daemon, endpoint, transport="unix")
        server.start()
        payload = {
            "schema": "simplicio.code-worker-adapter/v1",
            "protocol": "simplicio.loop-worker/v1",
            "identity": {
                "coordinator_id": "agent-host", "session_id": "s1",
                "turn_id": "t1", "run_id": "r1", "goal_id": "g1",
            },
            "idempotency_key": "worker-key-1",
            "max_concurrency": 2,
            "tasks": [
                {"task_id": "implement", "role": "implementer", "depends_on": [],
                 "task_contract": "edit only through Runtime"},
                {"task_id": "review", "role": "reviewer", "depends_on": ["implement"],
                 "task_contract": "review the external diff"},
            ],
        }
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.connect(endpoint)
                reader = stream.makefile("rb")
                first = _request(stream, reader, 1, "worker_delegate", payload)
                replay = _request(stream, reader, 2, "worker_delegate", payload)
                assert replay == first
                status = _request(stream, reader, 3, "worker_status", {
                    "workflow_id": first["workflow_id"], "after_sequence": 0,
                })
                assert status["next_sequence"] == 2
                assert [event["state"] for event in status["events"]] == ["waiting", "waiting"]
                cancelled = _request(stream, reader, 4, "worker_cancel", {
                    "workflow_id": first["workflow_id"], "idempotency_key": "cancel-1",
                    "reason": "operator stop", "revoke_mutation_authority": True,
                })
                assert cancelled["workflow_id"] == first["workflow_id"]
                after_cancel = _request(stream, reader, 5, "worker_status", {
                    "workflow_id": first["workflow_id"], "after_sequence": 2,
                })
                assert [event["state"] for event in after_cancel["events"]] == ["cancelled", "cancelled"]
                reader.close()
        finally:
            server.shutdown()
            daemon.stop()
        restarted = HubDaemon(lock)
        restarted.start()
        restarted_server = HubSocketServer(restarted, endpoint, transport="unix")
        restarted_server.start()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.connect(endpoint)
                reader = stream.makefile("rb")
                status = _request(stream, reader, 6, "worker_status", {
                    "workflow_id": first["workflow_id"], "after_sequence": 2,
                })
                assert status["events"][0]["state"] == "cancelled"
                reader.close()
        finally:
            restarted_server.shutdown()
            restarted.stop()
