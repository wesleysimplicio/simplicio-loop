import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from simplicio_loop.hub_daemon import HubDaemon, HubEnvelope
from simplicio_loop.process_supervisor import ProcessSpec, PythonProcessAdapter
import simplicio_loop.process_supervisor_rust as psr
from simplicio_loop.process_supervisor_rust import RustProcessAdapter, get_process_adapter, rust_binary_path, run_with_fallback


def _write_stub_binary(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "stub-supervisor"
    script.write_text(f"#!{sys.executable}\n{textwrap.dedent(body)}")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


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


def test_rust_binary_path_falls_back_to_shutil_which(monkeypatch) -> None:
    monkeypatch.setattr(psr, "_BIN_CANDIDATES", (Path("/nonexistent/one"), Path("/nonexistent/two")))
    monkeypatch.setattr(psr.shutil, "which", lambda name: "/usr/local/bin/simplicio-supervisor")
    assert psr.rust_binary_path() == Path("/usr/local/bin/simplicio-supervisor")


def test_rust_binary_path_returns_none_when_nothing_found(monkeypatch) -> None:
    monkeypatch.setattr(psr, "_BIN_CANDIDATES", (Path("/nonexistent/one"),))
    monkeypatch.setattr(psr.shutil, "which", lambda name: None)
    assert psr.rust_binary_path() is None


def test_rust_adapter_run_raises_when_binary_missing() -> None:
    adapter = RustProcessAdapter(binary=Path("/nonexistent/simplicio-supervisor"))
    adapter.binary = None
    with pytest.raises(RuntimeError, match="simplicio-supervisor binary not found"):
        adapter.run(ProcessSpec(("echo", "hi")))


def test_rust_adapter_run_swallows_on_spawned_exception(tmp_path) -> None:
    stub = _write_stub_binary(
        tmp_path,
        """
        import json, sys
        json.loads(sys.stdin.read())
        sys.stdout.write(json.dumps({"exit_code": 0, "stdout": "ok", "stderr": "", "duration_ms": 1}))
        """,
    )
    adapter = RustProcessAdapter(binary=stub)

    def _boom(_process) -> None:
        raise RuntimeError("registry unavailable")

    result = adapter.run(ProcessSpec(("echo", "hi")), on_spawned=_boom)
    assert result.returncode == 0
    assert result.stdout == "ok"


def test_rust_adapter_run_kills_on_timeout(tmp_path) -> None:
    stub = _write_stub_binary(
        tmp_path,
        """
        import sys, time
        sys.stdin.read()
        time.sleep(5)
        """,
    )
    adapter = RustProcessAdapter(binary=stub)
    with pytest.raises(subprocess.TimeoutExpired):
        adapter.run(ProcessSpec(("echo", "hi")), timeout_seconds=0.2)


def test_run_with_fallback_uses_python_adapter_and_awaits_coroutine(monkeypatch) -> None:
    monkeypatch.setattr(psr, "get_process_adapter", lambda: PythonProcessAdapter())
    result = run_with_fallback(ProcessSpec((sys.executable, "-c", "print('fallback-ok')")))
    assert result.returncode == 0
    assert "fallback-ok" in result.stdout
