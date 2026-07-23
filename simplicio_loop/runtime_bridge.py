"""Hub-owned bridge to an already-installed Simplicio Runtime MCP process.

The Loop Hub owns the lifecycle of this bridge, while the Runtime process owns
filesystem/process/effect policy.  The bridge is deliberately lazy and
fail-closed: no Runtime is started until Code submits an effect, and no model
or inference command is selected here.
"""

from __future__ import annotations

import json
import hashlib
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


RUNTIME_BRIDGE_SCHEMA = "simplicio.loop-runtime-bridge/v1"
RUNTIME_MCP_PROTOCOL = "2024-11-05"
RUNTIME_CALL_SCHEMA = "simplicio.loop-runtime-call/v1"


class RuntimeBridgeError(RuntimeError):
    """A Runtime bridge operation could not be delivered or verified."""


class _RuntimeProcess:
    def __init__(self, binary: str, workspace: Path) -> None:
        self.process = subprocess.Popen(
            [binary, "serve", "--mcp", "--stdio", "--json"],
            cwd=str(workspace),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._next_id = 1
        self._lock = threading.RLock()
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._reader = threading.Thread(target=self._read_stdout, name="loop-runtime-mcp-reader", daemon=True)
        self._reader.start()
        self._initialize()

    def _read_stdout(self) -> None:
        if self.process.stdout is None:
            self._lines.put(None)
            return
        for line in self.process.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _request(self, method: str, params: Mapping[str, Any], *, timeout: float = 10.0) -> Dict[str, Any]:
        with self._lock:
            if self.process.poll() is not None or self.process.stdin is None or self.process.stdout is None:
                raise RuntimeBridgeError("Runtime MCP process is not running")
            request_id = self._next_id
            self._next_id += 1
            self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id,
                                                  "method": method, "params": dict(params)}) + "\n")
            self.process.stdin.flush()
            try:
                line = self._lines.get(timeout=timeout)
            except queue.Empty as exc:
                raise RuntimeBridgeError("Runtime MCP response timed out") from exc
            if not line:
                raise RuntimeBridgeError("Runtime MCP closed stdout before returning a response")
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeBridgeError("Runtime MCP returned invalid JSON") from exc
            if response.get("id") != request_id:
                raise RuntimeBridgeError("Runtime MCP response id does not match request")
            if "error" in response:
                raise RuntimeBridgeError(str(response["error"]))
            result = response.get("result")
            if not isinstance(result, dict):
                raise RuntimeBridgeError("Runtime MCP response omitted an object result")
            return result

    def _initialize(self) -> None:
        # A cold Runtime may load its policy/configuration before it can emit
        # the MCP initialize response.  Keep effect calls bounded separately,
        # but do not misclassify a slow first boot as an unavailable Runtime.
        result = self._request("initialize", {
            "protocolVersion": RUNTIME_MCP_PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "simplicio-loop-hub", "version": RUNTIME_BRIDGE_SCHEMA},
        }, timeout=60.0)
        if result.get("protocolVersion") != RUNTIME_MCP_PROTOCOL:
            raise RuntimeBridgeError("Runtime MCP protocol version mismatch")
        # Notifications have no response.  The Runtime accepts the normal
        # initialize/initialized sequence; write it explicitly without
        # consuming a response frame.
        if self.process.stdin is None:
            raise RuntimeBridgeError("Runtime MCP stdin is unavailable")
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.process.stdin.flush()

    def call_tool(self, name: str, arguments: Mapping[str, Any], *, timeout: float = 10.0) -> Dict[str, Any]:
        result = self._request(
            "tools/call", {"name": name, "arguments": dict(arguments)}, timeout=timeout,
        )
        content = result.get("content")
        if not isinstance(content, list) or not content or not isinstance(content[0], dict):
            raise RuntimeBridgeError("Runtime MCP tools/call omitted content")
        text = content[0].get("text")
        if not isinstance(text, str):
            raise RuntimeBridgeError("Runtime MCP tools/call omitted text content")
        if result.get("isError") is True:
            raise RuntimeBridgeError(text)
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeBridgeError("Runtime MCP tool returned non-JSON text") from exc
        if not isinstance(value, dict):
            raise RuntimeBridgeError("Runtime MCP tool returned a non-object payload")
        return value

    def close(self) -> None:
        with self._lock:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)


class RuntimeBridge:
    """Lazy, one-process-per-workspace Runtime owner for the Loop Hub."""

    def __init__(self, binary: Optional[str] = None) -> None:
        self.binary = binary or os.environ.get("SIMPLICIO_RUNTIME_BIN") or "simplicio-runtime"
        self._lock = threading.RLock()
        self._processes: Dict[str, _RuntimeProcess] = {}

    def _process_for_workspace(self, workspace_path: Path) -> _RuntimeProcess:
        """Return the single lazy MCP process owned for this workspace."""
        key = str(workspace_path)
        process = self._processes.get(key)
        if process is None or process.process.poll() is not None:
            if process is not None:
                process.close()
            process = _RuntimeProcess(self.binary, workspace_path)
            self._processes[key] = process
        return process

    @staticmethod
    def _effect_transaction(*, tool: str, arguments: Mapping[str, Any],
                            relative_cwd: Path, idempotency_key: str,
                            timeout_ms: int) -> Dict[str, Any]:
        try:
            action = json.dumps(
                {"tool": tool, "arguments": dict(arguments)},
                sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise RuntimeBridgeError("runtime_call arguments must be JSON-compatible") from exc
        return {
            "schema": "simplicio.effect-transaction/v1",
            "executor": "simplicio-runtime",
            "request": {
                "schema": "simplicio.effect-request/v1",
                "capability": tool,
                "identity": {
                    "session": "loop-hub-runtime",
                    "turn": "runtime-bridge",
                    "tool_call": idempotency_key,
                    "attempt": "1",
                    "transaction": idempotency_key,
                },
                "authority": "loop-hub-runtime-bridge",
                "policy_receipt": "loop-hub-runtime-policy-v1",
                "idempotency_key": idempotency_key,
                "action_digest": "sha256:" + hashlib.sha256(action).hexdigest(),
                "write_set": ["repo:" + str(relative_cwd)],
                "preconditions": ["workspace-authorized"],
                "lease": {"id": "loop-hub-runtime-lease", "fence": 1},
                "deadline_ms": int(time.time() * 1000) + max(int(timeout_ms), 1),
                "cancellation": "safe_boundary_only",
                "validation_plan": "loop-hub-runtime-call-validation-v1",
                "rollback_plan": "runtime-call-boundary",
                "redaction_plan": "runtime-default-redaction",
            },
        }

    def execute(self, workspace: str, argv: list[str], cwd: str = ".",
                env: Optional[Mapping[str, str]] = None, timeout_ms: int = 120_000,
                max_output_bytes: int = 4 * 1024 * 1024,
                idempotency_key: str = "") -> Dict[str, Any]:
        if not workspace or not argv or not idempotency_key:
            raise RuntimeBridgeError("workspace, argv and idempotency_key are required")
        workspace_path = Path(workspace).expanduser().resolve()
        if not workspace_path.is_dir():
            raise RuntimeBridgeError("Runtime workspace does not exist")
        relative_cwd = Path(cwd)
        if relative_cwd.is_absolute() or ".." in relative_cwd.parts:
            raise RuntimeBridgeError("Runtime cwd must stay workspace-relative")
        with self._lock:
            process = self._process_for_workspace(workspace_path)
            return process.call_tool("simplicio_exec", {
                "repo": str(workspace_path), "cwd": str(relative_cwd), "argv": list(argv),
                "env": dict(env or {}), "timeout_ms": min(max(int(timeout_ms), 1), 120_000),
                "max_output_bytes": min(max(int(max_output_bytes), 1), 4 * 1024 * 1024),
                "idempotency_key": idempotency_key,
                "__runtime_effect_transaction": {
                    "schema": "simplicio.effect-transaction/v1",
                    "executor": "simplicio-runtime",
                    "request": {
                        "schema": "simplicio.effect-request/v1",
                        "capability": "simplicio_exec",
                        "identity": {
                            "session": "loop-hub-runtime",
                            "turn": "runtime-bridge",
                            "tool_call": idempotency_key,
                            "attempt": "1",
                            "transaction": idempotency_key,
                        },
                        "authority": "loop-hub-runtime-bridge",
                        "policy_receipt": "loop-hub-runtime-policy-v1",
                        "idempotency_key": idempotency_key,
                        "action_digest": "sha256:" + hashlib.sha256(
                            json.dumps(argv, sort_keys=True).encode("utf-8")
                        ).hexdigest(),
                        "write_set": ["repo:" + str(relative_cwd)],
                        "preconditions": ["workspace-authorized"],
                        "lease": {"id": "loop-hub-runtime-lease", "fence": 1},
                        "deadline_ms": int(time.time() * 1000) + max(int(timeout_ms), 1),
                        "cancellation": "safe_boundary_only",
                        "validation_plan": "loop-hub-runtime-validation-v1",
                        "rollback_plan": "runtime-process-boundary",
                        "redaction_plan": "runtime-default-redaction",
                    },
                },
            })

    def runtime_call(self, workspace: str, tool: str, arguments: Mapping[str, Any],
                     *, cwd: str = ".", timeout_ms: int = 120_000,
                     idempotency_key: str = "") -> Dict[str, Any]:
        """Call one allowlisted Runtime MCP tool through the existing process.

        The caller supplies the versioned tool arguments, while the bridge owns
        the effect transaction.  A caller cannot replace that transaction or
        cause a second Runtime process to be started for the same workspace.
        """
        if not workspace or not tool or not idempotency_key:
            raise RuntimeBridgeError("workspace, tool and idempotency_key are required")
        if not tool.startswith("simplicio_") or any(not (char.isalnum() or char in "_.-") for char in tool):
            raise RuntimeBridgeError("runtime_call tool must be a safe simplicio_ tool name")
        if not isinstance(arguments, Mapping):
            raise RuntimeBridgeError("runtime_call arguments must be an object")
        if "__runtime_effect_transaction" in arguments:
            raise RuntimeBridgeError("runtime_call transaction is bridge-owned")
        workspace_path = Path(workspace).expanduser().resolve()
        if not workspace_path.is_dir():
            raise RuntimeBridgeError("Runtime workspace does not exist")
        relative_cwd = Path(cwd)
        if relative_cwd.is_absolute() or ".." in relative_cwd.parts:
            raise RuntimeBridgeError("Runtime cwd must stay workspace-relative")
        try:
            bounded_timeout_ms = min(max(int(timeout_ms), 1), 120_000)
        except (TypeError, ValueError) as exc:
            raise RuntimeBridgeError("runtime_call timeout_ms must be an integer") from exc
        request_arguments = dict(arguments)
        request_arguments["__runtime_effect_transaction"] = self._effect_transaction(
            tool=tool,
            arguments=arguments,
            relative_cwd=relative_cwd,
            idempotency_key=idempotency_key,
            timeout_ms=bounded_timeout_ms,
        )
        with self._lock:
            process = self._process_for_workspace(workspace_path)
            return process.call_tool(tool, request_arguments, timeout=bounded_timeout_ms / 1000.0)

    def close(self) -> None:
        with self._lock:
            for process in self._processes.values():
                process.close()
            self._processes.clear()


__all__ = ["RUNTIME_BRIDGE_SCHEMA", "RUNTIME_CALL_SCHEMA", "RuntimeBridge", "RuntimeBridgeError"]
