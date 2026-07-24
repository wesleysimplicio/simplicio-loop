import json
import queue
import threading
import time
from pathlib import Path

import pytest

from simplicio_loop.runtime_bridge import (
    RUNTIME_MCP_PROTOCOL,
    RuntimeBridgeError,
    RuntimeBridgeBackpressure,
    RuntimeBridge,
    RuntimeBridgeCancelled,
    RuntimeBridgeRecoveryUnknown,
    RuntimeBridgeTimeout,
    _RuntimeProcess,
)


class _FakeStdout:
    def __init__(self, lines):
        self.lines = lines

    def __iter__(self):
        return iter(self.lines)


class _FakePopen:
    def __init__(self, *, lines=None):
        self.stdout = _FakeStdout(lines or [])
        self.stdin = self
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def write(self, text):
        return len(text)

    def flush(self):
        return None

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _process_for_unit(fake):
    process = _RuntimeProcess.__new__(_RuntimeProcess)
    process.process = fake
    process._next_id = 1
    process._write_lock = threading.Lock()
    process._state_lock = threading.Lock()
    process._pending = {}
    process._closed = threading.Event()
    return process


def test_reader_routes_only_matching_ids_and_fails_pending_at_eof():
    process = _process_for_unit(_FakePopen(lines=[
        "not-json\n",
        json.dumps({"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}) + "\n",
        json.dumps({"jsonrpc": "2.0", "method": "notification"}) + "\n",
    ]))
    matched = queue.Queue(maxsize=1)
    abandoned = queue.Queue(maxsize=1)
    process._pending = {7: matched, 8: abandoned}
    process._read_stdout()
    assert matched.get_nowait()["result"] == {"ok": True}
    assert abandoned.get_nowait() is None
    assert process._closed.is_set()


def test_request_correlates_success_and_tool_decodes_json():
    fake = _FakePopen()
    process = _process_for_unit(fake)

    def write(text):
        request_id = json.loads(text)["id"]
        process._pending[request_id].put({
            "id": request_id,
            "result": {"content": [{"text": json.dumps({"ok": True})}]},
        })

    fake.write = write
    assert process.call_tool("simplicio_status", {}) == {"ok": True}


def test_request_rejects_transport_timeout_and_mismatched_result():
    fake = _FakePopen()
    process = _process_for_unit(fake)
    with pytest.raises(RuntimeBridgeTimeout):
        process._request("slow", {}, timeout=0.001)

    def mismatch(text):
        process._pending[json.loads(text)["id"]].put({"id": 999, "result": {}})

    fake.write = mismatch
    with pytest.raises(RuntimeBridgeRecoveryUnknown):
        process._request("wrong", {}, timeout=0.1)


def test_initialize_and_close_cover_protocol_and_process_shutdown():
    fake = _FakePopen()
    process = _process_for_unit(fake)
    process._request = lambda *_args, **_kwargs: {"protocolVersion": RUNTIME_MCP_PROTOCOL}
    process._initialize()
    process.close()
    assert fake.terminated and fake.returncode == 0


def test_tool_result_errors_are_structured():
    fake = _FakePopen()
    process = _process_for_unit(fake)

    def error(text):
        request_id = json.loads(text)["id"]
        process._pending[request_id].put({"id": request_id, "error": {"message": "bad"}})

    fake.write = error
    with pytest.raises(RuntimeBridgeError) as exc:
        process._request("bad", {}, timeout=0.1)
    assert exc.value.code == "runtime_error"


def test_process_constructor_starts_reader_and_close(tmp_path, monkeypatch):
    fake = _FakePopen()
    monkeypatch.setattr("simplicio_loop.runtime_bridge.subprocess.Popen", lambda *args, **kwargs: fake)
    monkeypatch.setattr(_RuntimeProcess, "_initialize", lambda self: None)
    process = _RuntimeProcess("unused", Path(tmp_path))
    process.close()
    assert fake.terminated


def test_reader_handles_missing_stdout_and_dead_process():
    fake = _FakePopen()
    fake.stdout = None
    process = _process_for_unit(fake)
    process._pending = {1: queue.Queue(maxsize=1)}
    process._read_stdout()
    assert process._pending == {}
    fake.returncode = 1
    with pytest.raises(RuntimeBridgeRecoveryUnknown):
        process._request("dead", {})


@pytest.mark.parametrize("result", [
    {},
    {"content": [{}]},
    {"content": [{"text": 1}]},
    {"content": [{"text": "bad"}], "isError": True},
    {"content": [{"text": "not-json"}]},
    {"content": [{"text": "[]"}]},
])
def test_call_tool_rejects_invalid_payloads(result):
    process = _process_for_unit(_FakePopen())
    process._request = lambda *_args, **_kwargs: result
    with pytest.raises(RuntimeBridgeError):
        process.call_tool("simplicio_status", {})


def test_bridge_validation_effect_digest_and_close(tmp_path):
    bridge = RuntimeBridge(binary="unused")
    with pytest.raises(RuntimeBridgeError):
        bridge.runtime_call("", "simplicio_status", {}, idempotency_key="x")
    with pytest.raises(RuntimeBridgeError):
        bridge.runtime_call(str(tmp_path), "bad", {}, idempotency_key="x")
    with pytest.raises(RuntimeBridgeError):
        bridge.runtime_call(str(tmp_path), "simplicio_status", [], idempotency_key="x")
    with pytest.raises(RuntimeBridgeError):
        bridge.runtime_call(str(tmp_path), "simplicio_status", {"__runtime_effect_transaction": {}}, idempotency_key="x")
    with pytest.raises(RuntimeBridgeError):
        bridge.runtime_call(str(tmp_path), "simplicio_status", {"value": object()}, idempotency_key="x")
    with pytest.raises(RuntimeBridgeError):
        bridge.runtime_call(str(tmp_path), "simplicio_status", {}, cwd="../outside", idempotency_key="x")
    with pytest.raises(RuntimeBridgeError):
        bridge.execute(str(tmp_path), [], idempotency_key="x")
    with pytest.raises(RuntimeBridgeError):
        bridge.execute(str(tmp_path), ["echo"], idempotency_key="")
    session = bridge._session_for_workspace(tmp_path)
    session.state = "ready"
    fake = _FakePopen()
    session.process = _process_for_unit(fake)
    bridge.close()
    assert fake.terminated


def test_bridge_global_admission_can_cancel_and_timeout():
    bridge = RuntimeBridge(max_global_inflight=1)
    bridge._global_inflight = 1
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(RuntimeBridgeCancelled):
        bridge._acquire_global(deadline=time.monotonic() + 1, cancel_event=cancelled)
    with pytest.raises(RuntimeBridgeTimeout):
        bridge._acquire_global(deadline=time.monotonic() - 1, cancel_event=None)
    bridge = RuntimeBridge(max_global_inflight=1, max_global_queue=1)
    bridge._global_waiters = 1
    with pytest.raises(RuntimeBridgeBackpressure):
        bridge._acquire_global(deadline=time.monotonic() + 1, cancel_event=None)
