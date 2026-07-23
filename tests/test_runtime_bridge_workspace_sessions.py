import threading
import time
from pathlib import Path

from simplicio_loop.runtime_bridge import RuntimeBridge


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
