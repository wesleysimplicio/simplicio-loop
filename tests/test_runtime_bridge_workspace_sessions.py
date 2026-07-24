import threading
import time
from pathlib import Path

from simplicio_loop.runtime_bridge import (
    RuntimeBridge,
    RuntimeBridgeBackpressure,
    RuntimeBridgeCancelled,
    RuntimeBridgeRecoveryUnknown,
)


class _FakeProcess:
    def __init__(self, entered, release):
        self.process = self
        self.entered = entered
        self.release = release

    def poll(self):
        return None

    def call_tool(self, *_args, **_kwargs):
        self.entered.set()
        self.release.wait(2)
        return {"ok": True}

    def close(self):
        return None


def test_different_workspaces_do_not_share_global_bridge_lock(tmp_path):
    bridge = RuntimeBridge(binary="unused")
    release = threading.Event()
    first_entered = threading.Event()
    second_entered = threading.Event()
    processes = {}

    def fake_process(path: Path):
        return processes.setdefault(str(path), _FakeProcess(first_entered if not processes else second_entered, release))

    bridge._process_for_workspace = fake_process  # type: ignore[method-assign]
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    results = []
    threads = [threading.Thread(target=lambda path=path: results.append(bridge.runtime_call(str(path), "simplicio_status", {}, idempotency_key=str(path)))) for path in (left, right)]
    threads[0].start()
    assert first_entered.wait(1)
    threads[1].start()
    assert second_entered.wait(1), "a second workspace must not wait on the first workspace session"
    release.set()
    for thread in threads:
        thread.join(2)
    assert len(results) == 2


def test_same_workspace_uses_one_bounded_session(tmp_path):
    bridge = RuntimeBridge(binary="unused")
    calls = 0
    lock = threading.Lock()
    entered = threading.Event()
    release = threading.Event()

    class Process(_FakeProcess):
        def call_tool(self, *_args, **_kwargs):
            nonlocal calls
            with lock:
                calls += 1
            entered.set()
            release.wait(2)
            return {"ok": True}

    process = Process(entered, release)
    bridge._process_for_workspace = lambda _path: process  # type: ignore[method-assign]
    workspace = tmp_path / "same"
    workspace.mkdir()
    results = []
    first = threading.Thread(target=lambda: results.append(bridge.runtime_call(str(workspace), "simplicio_status", {}, idempotency_key="one")))
    second = threading.Thread(target=lambda: results.append(bridge.runtime_call(str(workspace), "simplicio_status", {}, idempotency_key="two")))
    first.start()
    assert entered.wait(1)
    second.start()
    time.sleep(0.05)
    assert calls == 1
    release.set()
    first.join(2)
    second.join(2)
    assert calls == 2 and len(results) == 2


def test_queued_call_can_be_cancelled_without_invoking_runtime(tmp_path):
    bridge = RuntimeBridge(binary="unused", max_queue_per_workspace=2)
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    class Process(_FakeProcess):
        def call_tool(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            entered.set()
            release.wait(2)
            return {"ok": True}

    process = Process(entered, release)
    bridge._process_for_workspace = lambda _path: process  # type: ignore[method-assign]
    workspace = tmp_path / "cancel"
    workspace.mkdir()
    first = threading.Thread(target=lambda: bridge.runtime_call(
        str(workspace), "simplicio_exec", {}, idempotency_key="first"))
    first.start()
    assert entered.wait(1)
    cancelled = threading.Event()
    error = []

    def queued_call():
        try:
            bridge.runtime_call(str(workspace), "simplicio_exec", {},
                                idempotency_key="second", cancel_event=cancelled)
        except RuntimeBridgeCancelled as exc:
            error.append(exc)

    second = threading.Thread(target=queued_call)
    second.start()
    time.sleep(0.05)
    cancelled.set()
    second.join(1)
    release.set()
    first.join(1)
    assert len(error) == 1 and error[0].code == "cancelled"
    assert calls == 1


def test_workspace_queue_rejects_above_bound(tmp_path):
    bridge = RuntimeBridge(binary="unused", max_queue_per_workspace=1)
    entered = threading.Event()
    release = threading.Event()

    class Process(_FakeProcess):
        def call_tool(self, *_args, **_kwargs):
            entered.set()
            release.wait(2)
            return {"ok": True}

    process = Process(entered, release)
    bridge._process_for_workspace = lambda _path: process  # type: ignore[method-assign]
    workspace = tmp_path / "bounded"
    workspace.mkdir()
    first = threading.Thread(target=lambda: bridge.runtime_call(
        str(workspace), "simplicio_exec", {}, idempotency_key="first"))
    first.start()
    assert entered.wait(1)
    queued = threading.Thread(target=lambda: bridge.runtime_call(
        str(workspace), "simplicio_exec", {}, idempotency_key="queued"))
    queued.start()
    for _ in range(100):
        snapshot = bridge.status(str(workspace))["sessions"][0]
        if snapshot["queue_depth"] == 1:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("queued call did not reach the bounded admission queue")
    try:
        bridge.runtime_call(str(workspace), "simplicio_exec", {},
                            idempotency_key="rejected", timeout_ms=50)
    except RuntimeBridgeBackpressure as exc:
        assert exc.code == "backpressure"
    else:
        raise AssertionError("queue saturation must be observable")
    release.set()
    first.join(1)
    queued.join(1)
    assert bridge.status(str(workspace))["sessions"][0]["max_queue"] == 1


def test_safe_reads_can_share_a_bounded_workspace_session(tmp_path):
    bridge = RuntimeBridge(binary="unused", max_inflight_per_workspace=2,
                           safe_read_tools={"simplicio_status"})
    both_entered = threading.Event()
    release = threading.Event()
    active = 0
    max_active = 0
    lock = threading.Lock()

    class Process(_FakeProcess):
        def call_tool(self, *_args, **_kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            with lock:
                if active == 2:
                    both_entered.set()
            release.wait(2)
            with lock:
                active -= 1
            return {"ok": True}

    process = Process(threading.Event(), release)
    bridge._process_for_workspace = lambda _path: process  # type: ignore[method-assign]
    workspace = tmp_path / "reads"
    workspace.mkdir()
    threads = [threading.Thread(target=lambda key=key: bridge.runtime_call(
        str(workspace), "simplicio_status", {}, idempotency_key=key))
               for key in ("read-a", "read-b")]
    for thread in threads:
        thread.start()
    assert both_entered.wait(1)
    release.set()
    for thread in threads:
        thread.join(1)
    assert max_active == 2


def test_transport_recovery_increments_generation_without_replay(tmp_path, monkeypatch):
    import simplicio_loop.runtime_bridge as module

    class Process:
        created = 0

        def __init__(self, *_args):
            self.process = self
            self.dead = False
            Process.created += 1
            self.fail = Process.created == 1

        def poll(self):
            return 1 if self.dead else None

        def call_tool(self, *_args, **_kwargs):
            if self.fail:
                self.fail = False
                raise RuntimeBridgeRecoveryUnknown("crashed")
            return {"ok": True}

        def close(self):
            self.dead = True

    monkeypatch.setattr(module, "_RuntimeProcess", Process)
    bridge = RuntimeBridge(binary="unused")
    workspace = tmp_path / "recovery"
    workspace.mkdir()
    try:
        bridge.runtime_call(str(workspace), "simplicio_exec", {}, idempotency_key="uncertain")
    except RuntimeBridgeRecoveryUnknown as exc:
        assert exc.code == "recovery_unknown"
    else:
        raise AssertionError("uncertain calls must not be replayed")
    assert bridge.runtime_call(str(workspace), "simplicio_exec", {},
                               idempotency_key="new-generation")["ok"] is True
    status = bridge.status(str(workspace))["sessions"][0]
    assert status["generation"] == 2 and status["reconnects"] == 1
