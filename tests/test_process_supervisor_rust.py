import tempfile
from pathlib import Path

import pytest

from simplicio_loop.hub_daemon import HubDaemon, HubEnvelope
from simplicio_loop.process_supervisor import PythonProcessAdapter
import simplicio_loop.process_supervisor_rust as psr
from simplicio_loop.process_supervisor_rust import RustProcessAdapter, get_process_adapter, rust_binary_path


def test_rust_adapter_reports_unavailable_for_nonexistent_binary() -> None:
    adapter = RustProcessAdapter(binary=Path("/nonexistent/simplicio-supervisor"))
    assert adapter.available is False


def test_get_process_adapter_falls_back_to_python_when_binary_absent(monkeypatch) -> None:
    monkeypatch.setattr(psr, "rust_binary_path", lambda: None)
    adapter = get_process_adapter()
    assert isinstance(adapter, PythonProcessAdapter)


def test_get_process_adapter_never_crashes_regardless_of_backend() -> None:
    adapter = get_process_adapter()
    assert isinstance(adapter, (RustProcessAdapter, PythonProcessAdapter))


def test_hub_execute_runs_the_real_rust_binary_when_built() -> None:
    binary = rust_binary_path()
    if binary is None:
        pytest.skip("simplicio-supervisor binary not built (run cargo build --release in rust/simplicio-supervisor)")

    with tempfile.TemporaryDirectory() as tmp:
        daemon = HubDaemon(lock_path=str(Path(tmp) / "hub.lock"))
        daemon.start()
        try:
            envelope = HubEnvelope(
                request_id="req-1",
                method="execute",
                payload={
                    "process_spec": {
                        "argv": ["echo", "hub-rust-e2e"],
                        "timeout_seconds": 5.0,
                    }
                },
            )
            response = daemon.handle(envelope)
        finally:
            daemon.stop()

    assert response["ok"] is True
    assert response["backend"] == "rust"
    result = response["result"]
    assert result["returncode"] == 0
    assert "hub-rust-e2e" in result["stdout"]
