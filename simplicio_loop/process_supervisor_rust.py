"""Optional Rust/Tokio process-supervisor backend, with a clean fallback."""

import json
import asyncio
import inspect
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from .process_supervisor import ProcessResult, ProcessSpec

_CRATE_ROOT = Path(__file__).resolve().parent.parent / "rust" / "simplicio-supervisor"
_BIN_NAMES = ("simplicio-supervisor.exe", "simplicio-supervisor") if os.name == "nt" else ("simplicio-supervisor",)
_BIN_CANDIDATES = tuple(
    _CRATE_ROOT / "target" / profile / name
    for profile in ("release", "debug")
    for name in _BIN_NAMES
)


def rust_binary_path() -> Optional[Path]:
    for candidate in _BIN_CANDIDATES:
        if candidate.is_file():
            return candidate
    on_path = shutil.which("simplicio-supervisor")
    return Path(on_path) if on_path else None


def rust_backend_available() -> bool:
    return rust_binary_path() is not None


def run_with_fallback(
    spec: ProcessSpec,
    *,
    timeout_seconds: float = 60.0,
    on_spawned: Optional[Callable[[Any], None]] = None,
) -> ProcessResult:
    """Run through Rust when built, otherwise use the safe async Python adapter.

    ``on_spawned`` (issue #498/#516) is invoked synchronously with the real OS pid as soon as
    the child (Python) or the supervisor binary itself (Rust) is spawned, before completion is
    awaited -- the same additive hook contract as ``PythonProcessAdapter.run``'s ``on_spawned``,
    plumbed through here so callers of this single boundary (e.g. ``HubDaemon.handle``) can
    register the live pid with the enforcement registry regardless of which backend ran it.
    """
    adapter = get_process_adapter()
    if isinstance(adapter, RustProcessAdapter):
        result = adapter.run(spec, timeout_seconds=timeout_seconds, on_spawned=on_spawned)
    else:
        result = adapter.run(spec, on_spawned=on_spawned)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def backend_name() -> str:
    return "rust" if rust_backend_available() else "python-fallback"


class RustProcessAdapter:
    """Shells out to the compiled simplicio-supervisor binary when present."""

    def __init__(self, binary: Optional[Path] = None) -> None:
        self.binary = binary if binary is not None else rust_binary_path()

    @property
    def available(self) -> bool:
        return self.binary is not None and self.binary.is_file()

    def run(
        self,
        spec: ProcessSpec,
        *,
        timeout_seconds: float = 60.0,
        on_spawned: Optional[Callable[[Any], None]] = None,
    ) -> ProcessResult:
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
        process = subprocess.Popen(
            [str(self.binary)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if on_spawned is not None:
            try:
                on_spawned(process)
            except Exception:
                pass
        try:
            stdout, stderr = process.communicate(
                input=json.dumps(payload), timeout=timeout_seconds
            )
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        result = json.loads(stdout)
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
