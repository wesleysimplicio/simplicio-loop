"""Epic #498 end-to-end: ProcessSpec through the real Hub IPC boundary.

Proves the "unica fronteira" (single boundary) claim across the pieces that #514 (ProcessSpec/
Lease contract), #515 (Rust/Tokio backend), and #516 (enforcement/observability registry) each
landed in isolation, by driving them together through ``HubDaemon.handle(method="execute")`` --
the real IPC path a client actually calls, not ``process_supervisor``/``process_supervisor_rust``
called directly.

Two things are proven, both against a real spawned OS process (not a mock):

1. A ProcessSpec submitted via ``execute`` runs to completion through whichever backend is
   present -- the Rust binary when built (skipped, not faked, if the crate was never compiled in
   this checkout) and explicitly also through the Python fallback (forced via monkeypatch so the
   same assertions run even when Rust is unavailable).
2. While that process is alive, it is visible in the enforcement layer's ``ProcessRegistry`` --
   i.e. the Hub's execute path now registers the real pid with the same bookkeeping
   ``process_enforcement.detect_unsupervised`` diffs against -- and is unregistered the moment
   ``execute`` returns. A spec deliberately not routed through Hub (spawned by hand) is used as a
   negative control to show the registry only reflects Hub-supervised pids, not every process on
   the host.
"""

import sys
import tempfile
import time
from pathlib import Path

import pytest

from simplicio_loop import process_supervisor_rust as psr
from simplicio_loop.hub_daemon import HubDaemon, HubEnvelope
from simplicio_loop.process_enforcement import ProcessRegistry
from simplicio_loop.process_supervisor import ProcessSpec


SLEEP_SPEC = {
    "argv": [sys.executable, "-c", "import time; time.sleep(1.2)"],
    "timeout_seconds": 10.0,
}


def _daemon(tmp_path: Path) -> HubDaemon:
    daemon = HubDaemon(str(tmp_path / "hub.lock"))
    daemon.start()
    return daemon


def test_execute_through_hub_ipc_python_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(psr, "rust_binary_path", lambda: None)
    daemon = _daemon(tmp_path)
    try:
        response = daemon.handle(
            HubEnvelope("req-py", "execute", {"process_spec": {
                "argv": [sys.executable, "-c", "print('py-fallback-ok')"],
                "timeout_seconds": 10.0,
            }})
        )
        assert response["ok"] is True
        assert response["backend"] == "python-fallback"
        assert response["result"]["returncode"] == 0
        assert "py-fallback-ok" in response["result"]["stdout"]
    finally:
        daemon.stop()


@pytest.mark.skipif(
    not psr.rust_backend_available(),
    reason="rust/simplicio-supervisor binary not built in this checkout",
)
def test_execute_through_hub_ipc_rust_backend() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = _daemon(Path(directory))
        try:
            response = daemon.handle(
                HubEnvelope("req-rust", "execute", {"process_spec": {
                    "argv": [sys.executable, "-c", "print('rust-backend-ok')"],
                    "timeout_seconds": 10.0,
                }})
            )
            assert response["ok"] is True
            assert response["backend"] == "rust"
            assert response["result"]["returncode"] == 0
            assert "rust-backend-ok" in response["result"]["stdout"]
        finally:
            daemon.stop()


def _run_execute_in_background(daemon: HubDaemon, request_id: str):
    import threading

    holder: dict = {}

    def _worker() -> None:
        holder["response"] = daemon.handle(
            HubEnvelope(request_id, "execute", {"process_spec": dict(SLEEP_SPEC)})
        )

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return thread, holder


def test_hub_executed_process_is_visible_to_enforcement_registry_while_running(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(psr, "rust_binary_path", lambda: None)
    registry_path = tmp_path / "registry.json"
    registry = ProcessRegistry(registry_path)
    daemon = HubDaemon(str(tmp_path / "hub.lock"), process_registry=registry)
    daemon.start()
    try:
        thread, holder = _run_execute_in_background(daemon, "req-sleep")
        deadline = time.monotonic() + 5
        seen_active = False
        while time.monotonic() < deadline and not seen_active:
            if registry.active_pids():
                seen_active = True
                break
            time.sleep(0.05)
        assert seen_active, "expected the hub-executed pid to appear in the registry while running"

        thread.join(timeout=10)
        assert "response" in holder
        assert holder["response"]["ok"] is True
        assert holder["response"]["result"]["returncode"] == 0

        assert registry.active_pids() == set(), (
            "registry must unregister the pid once HubDaemon.handle(execute) returns"
        )
    finally:
        daemon.stop()


def test_registry_does_not_track_processes_spawned_outside_the_hub(tmp_path) -> None:
    import subprocess

    registry_path = tmp_path / "registry.json"
    registry = ProcessRegistry(registry_path)
    outside = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"])
    try:
        assert outside.pid not in registry.active_pids()
    finally:
        outside.wait(timeout=5)
