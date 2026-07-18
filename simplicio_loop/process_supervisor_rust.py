"""Optional Rust/Tokio process-supervisor backend, with a clean fallback."""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .process_supervisor import ProcessResult, ProcessSpec

_CRATE_ROOT = Path(__file__).resolve().parent.parent / "rust" / "simplicio-supervisor"
_BIN_CANDIDATES = (
    _CRATE_ROOT / "target" / "release" / "simplicio-supervisor",
    _CRATE_ROOT / "target" / "debug" / "simplicio-supervisor",
)


def rust_binary_path() -> Optional[Path]:
    for candidate in _BIN_CANDIDATES:
        if candidate.is_file():
            return candidate
    on_path = shutil.which("simplicio-supervisor")
    return Path(on_path) if on_path else None


def rust_backend_available() -> bool:
    return rust_binary_path() is not None


class RustProcessAdapter:
    """Shells out to the compiled simplicio-supervisor binary when present."""

    def __init__(self, binary: Optional[Path] = None) -> None:
        self.binary = binary if binary is not None else rust_binary_path()

    @property
    def available(self) -> bool:
        return self.binary is not None and self.binary.is_file()

    def run(self, spec: ProcessSpec, *, timeout_seconds: float = 60.0) -> ProcessResult:
        if self.binary is None:
            raise RuntimeError(
                "simplicio-supervisor binary not found; build rust/simplicio-supervisor "
                "or fall back to PythonProcessAdapter"
            )
        payload = {
            "argv": list(spec.argv),
            "cwd": spec.cwd,
            "env": dict(spec.env),
            "env_allowlist": list(spec.env_allowlist),
            "deadline_seconds": spec.timeout_seconds,
            "max_output_bytes": spec.max_output_bytes,
        }
        completed = subprocess.run(
            [str(self.binary)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        result = json.loads(completed.stdout)
        return ProcessResult(
            result.get("exit_code"),
            result.get("stdout", ""),
            result.get("stderr", ""),
            result.get("duration_ms", 0) / 1000.0,
            timed_out=bool(result.get("timed_out", False)),
            truncated=bool(result.get("truncated", False)),
            error_code=result.get("error") or "",
        )


def get_process_adapter():
    """Return the Rust adapter if built, else the pure-Python adapter."""
    from .process_supervisor import PythonProcessAdapter

    rust_adapter = RustProcessAdapter()
    if rust_adapter.available:
        return rust_adapter
    return PythonProcessAdapter()
