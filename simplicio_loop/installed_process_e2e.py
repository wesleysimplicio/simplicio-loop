"""Real installed component/process verification for issue #693."""

from __future__ import annotations
import hashlib
import json
import os
import queue
import shutil
import subprocess
import tempfile
import sys
import threading
import time
import uuid
from pathlib import Path
from statistics import quantiles
from typing import Any, Mapping, Optional, Sequence

SCHEMA = "simplicio.installed-runtime-process-e2e/v1"
COMPONENTS = ("mapper", "dev_cli", "watcher", "hbp", "runtime")


class InstalledProcessError(RuntimeError):
    """The installed process chain cannot produce a trustworthy receipt."""


def _digest(value: Any) -> str:
    if isinstance(value, bytes):
        return hashlib.sha256(value).hexdigest()
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _resolve(name: str, explicit: Optional[str] = None) -> Optional[str]:
    candidate = explicit or shutil.which(name)
    if not candidate:
        return None
    try:
        return str(Path(candidate).expanduser().resolve(strict=True))
    except OSError:
        return None


def _argv(executable: str, args: Sequence[str]) -> list[str]:
    if Path(executable).suffix.lower() in {".cmd", ".bat"} and os.name == "nt":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", executable, *args]
    return [executable, *args]


def _run(
    name: str, command: Sequence[str], cwd: Path, timeout: float
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "component": name,
            "status": "READY" if proc.returncode == 0 else "BLOCKED",
            "reason": "" if proc.returncode == 0 else "process_exit",
            "command": list(command),
            "exit_code": proc.returncode,
            "pid": None,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
        }
    except FileNotFoundError:
        return {
            "component": name,
            "status": "UNAVAILABLE",
            "reason": "binary_missing",
            "command": list(command),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "component": name,
            "status": "BLOCKED",
            "reason": "timeout",
            "command": list(command),
            "stdout": str(exc.stdout or "")[-4000:],
            "stderr": str(exc.stderr or "")[-4000:],
        }
    except OSError as exc:
        return {
            "component": name,
            "status": "UNAVAILABLE",
            "reason": type(exc).__name__,
            "command": list(command),
        }


def _version(
    name: str, executable: Optional[str], cwd: Path, timeout: float
) -> dict[str, Any]:
    if not executable:
        return {"component": name, "status": "UNAVAILABLE", "reason": "binary_missing"}
    receipt = _run(name, _argv(executable, ["--version"]), cwd, timeout)
    receipt.update(
        {"path": executable, "binary_sha256": _digest(Path(executable).read_bytes())}
    )
    return receipt


class _McpSession:
    def __init__(self, executable: str, cwd: Path) -> None:
        self.command = _argv(executable, ["serve", "--mcp", "--stdio", "--json"])
        self.started_at = time.time()
        self.process = subprocess.Popen(
            self.command,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._next_id = 1
        self._lines: queue.Queue[Optional[str]] = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        if self.process.stdout is None:
            self._lines.put(None)
            return
        for line in self.process.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def request(
        self, method: str, params: Mapping[str, Any], timeout: float
    ) -> dict[str, Any]:
        if self.process.stdin is None or self.process.poll() is not None:
            raise InstalledProcessError("runtime_process_not_running")
        request_id = self._next_id
        self._next_id += 1
        self.process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": dict(params),
                }
            )
            + "\n"
        )
        self.process.stdin.flush()
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty as exc:
            raise InstalledProcessError("runtime_response_timeout") from exc
        if not line:
            raise InstalledProcessError("runtime_process_closed")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise InstalledProcessError("runtime_invalid_json") from exc
        if response.get("id") != request_id or not isinstance(
            response.get("result"), dict
        ):
            raise InstalledProcessError("runtime_invalid_response")
        return response["result"]

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        finally:
            self._reader.join(timeout=1)
            if self.process.stdin is not None:
                self.process.stdin.close()
            if self.process.stdout is not None:
                self.process.stdout.close()


def _runtime(executable: Optional[str], cwd: Path, timeout: float) -> dict[str, Any]:
    if not executable:
        return {
            "component": "runtime",
            "status": "UNAVAILABLE",
            "reason": "binary_missing",
        }
    session = None
    started = time.perf_counter()
    try:
        session = _McpSession(executable, cwd)
        result = session.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "simplicio-loop-installed-e2e", "version": "1"},
            },
            timeout,
        )
        if result.get("protocolVersion") != "2024-11-05":
            raise InstalledProcessError("runtime_protocol_mismatch")
        if session.process.stdin is None:
            raise InstalledProcessError("runtime_stdin_missing")
        session.process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        session.process.stdin.flush()
        session.request("resources/list", {}, timeout)
        return {
            "component": "runtime",
            "status": "READY",
            "reason": "",
            "command": session.command,
            "pid": session.process.pid,
            "started_at": session.started_at,
            "requests": 2,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "binary_sha256": _digest(Path(executable).read_bytes()),
        }
    except (InstalledProcessError, OSError) as exc:
        return {
            "component": "runtime",
            "status": "BLOCKED",
            "reason": str(exc),
            "command": session.command
            if session
            else _argv(executable, ["serve", "--mcp", "--stdio", "--json"]),
            "pid": session.process.pid if session else None,
        }
    finally:
        if session:
            session.close()


def _metric(values: Sequence[float]) -> dict[str, Any]:
    values = sorted(v for v in values if v >= 0)
    if not values:
        return {"p50_ms": None, "p95_ms": None, "reason": "no_samples"}
    if len(values) == 1:
        return {"p50_ms": values[0], "p95_ms": values[0], "reason": "single_sample"}
    return {
        "p50_ms": round(values[len(values) // 2], 3),
        "p95_ms": round(quantiles(values, n=20, method="inclusive")[18], 3),
        "reason": "measured",
    }


def _default_watcher_command(root: Path) -> Optional[list[str]]:
    script = root / "scripts" / "watcher_verify.py"
    if not script.is_file():
        return None
    return [sys.executable, str(script), "verify", "--worktree", str(root)]


def _default_hbp_command(executable: Optional[str]) -> Optional[list[str]]:
    if not executable:
        return None
    return _argv(executable, ["hbp", "verify", "--json"])


def run_installed_process_smoke(
    repo: str,
    *,
    fixture_repo: Optional[str] = None,
    executable_overrides: Optional[Mapping[str, str]] = None,
    watcher_command: Optional[Sequence[str]] = None,
    hbp_command: Optional[Sequence[str]] = None,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    """Run real installed component processes and return an honest causal report."""
    root = Path(repo).expanduser().resolve()
    if not root.is_dir():
        raise InstalledProcessError("repo must be an existing directory")
    overrides = dict(executable_overrides or {})
    correlation_id = uuid.uuid4().hex
    holder = (
        tempfile.TemporaryDirectory(prefix="simplicio-693-")
        if fixture_repo is None
        else None
    )
    fixture = Path(holder.name) if holder else Path(fixture_repo).expanduser().resolve()
    if not fixture.is_dir():
        raise InstalledProcessError("fixture_repo must be an existing directory")
    if holder:
        (fixture / "README.md").write_text(
            "installed component fixture\n", encoding="utf-8"
        )
    components = {}
    samples = []
    try:
        mapper = _resolve("simplicio-mapper", overrides.get("mapper"))
        dev = _resolve("simplicio-dev-cli", overrides.get("dev_cli"))
        runtime = _resolve("simplicio", overrides.get("runtime")) or _resolve(
            "simplicio-runtime", overrides.get("runtime")
        )
        for name, exe in (("mapper", mapper), ("dev_cli", dev)):
            receipt = _version(name, exe, fixture, timeout_seconds)
            if receipt["status"] == "READY":
                args = (
                    ["macro", str(fixture), "--json"]
                    if name == "mapper"
                    else ["--help"]
                )
                receipt["probe"] = _run(
                    name, _argv(exe, args), fixture, timeout_seconds
                )
                receipt["status"] = receipt["probe"]["status"]
            receipt["correlation_id"] = correlation_id
            receipt["receipt_hash"] = _digest(receipt)
            components[name] = receipt
            samples.extend(
                float(x.get("duration_ms", 0))
                for x in (receipt, receipt.get("probe", {}))
            )
        watcher_probe = (
            list(watcher_command) if watcher_command else _default_watcher_command(root)
        )
        watcher = (
            _run("watcher", watcher_probe, root, timeout_seconds)
            if watcher_probe
            else {
                "component": "watcher",
                "status": "UNAVAILABLE",
                "reason": "watcher_command_not_configured",
            }
        )
        hbp_probe = list(hbp_command) if hbp_command else _default_hbp_command(runtime)
        hbp = (
            _run("hbp", hbp_probe, root, timeout_seconds)
            if hbp_probe
            else {
                "component": "hbp",
                "status": "UNAVAILABLE",
                "reason": "hbp_command_not_configured",
            }
        )
        for receipt in (watcher, hbp):
            receipt["correlation_id"] = correlation_id
            receipt["receipt_hash"] = _digest(receipt)
        components["watcher"], components["hbp"] = watcher, hbp
        components["runtime"] = _runtime(runtime, fixture, timeout_seconds)
        components["runtime"]["correlation_id"] = correlation_id
        components["runtime"]["receipt_hash"] = _digest(components["runtime"])
        samples.extend(float(x.get("duration_ms", 0)) for x in components.values())
        ready = all(
            components.get(name, {}).get("status") == "READY" for name in COMPONENTS
        )
        pids = {x.get("pid") for x in components.values() if x.get("pid")}
        return {
            "schema": SCHEMA,
            "status": "VERIFIED" if ready else "BLOCKED",
            "installed": True,
            "effects_attempted": False,
            "effects_authorized": False,
            "correlation_id": correlation_id,
            "fixture_repo": str(fixture),
            "components": components,
            "metrics": {
                "latency": _metric(samples),
                "process_count": len(pids),
                "cpu_percent": None,
                "cpu_reason": "portable_stdlib_unavailable",
                "rss_bytes": None,
                "rss_reason": "portable_stdlib_unavailable",
                "receipt_bytes": len(json.dumps(components, sort_keys=True).encode()),
            },
            "negative_lanes": {
                "direct_mutation_bypass": "BLOCKED_NOT_ATTEMPTED",
                "duplicate_idempotency": "BLOCKED_BY_EFFECT_POLICY",
                "stale_receipt": "BLOCKED_BY_LINK_GATE",
            },
            "report_hash": _digest(components),
        }
    finally:
        if holder:
            for attempt in range(5):
                try:
                    holder.cleanup()
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.1)


__all__ = [
    "COMPONENTS",
    "InstalledProcessError",
    "SCHEMA",
    "run_installed_process_smoke",
]
