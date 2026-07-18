from pathlib import Path

from simplicio_loop.process_supervisor import PythonProcessAdapter
import simplicio_loop.process_supervisor_rust as psr
from simplicio_loop.process_supervisor_rust import RustProcessAdapter, get_process_adapter


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
