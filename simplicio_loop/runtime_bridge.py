"""Hub-owned bridge to an installed Simplicio Runtime MCP process.

The bridge owns bounded admission and process lifecycle, while Runtime owns
effect policy.  Sessions are keyed by canonical workspace and an uncertain
request is never replayed during recovery.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Mapping, Optional, Set, Tuple

from .runtime_binary import (
    RuntimeBinaryError,
    resolve_and_probe_simplicio_binary,
    runtime_preflight,
)
from .canonical_plan import CanonicalPlan, canonical_plan_metadata


RUNTIME_BRIDGE_SCHEMA = "simplicio.loop-runtime-bridge/v1"
RUNTIME_MCP_PROTOCOL = "2024-11-05"
RUNTIME_CALL_SCHEMA = "simplicio.loop-runtime-call/v1"


class RuntimeBridgeError(RuntimeError):
    """A Runtime bridge operation could not be delivered or verified."""

    def __init__(self, message: str, *, code: str = "runtime_bridge_error",
                 receipt: Optional[Mapping[str, Any]] = None) -> None:
        self.code = code
        self.receipt = dict(receipt or {})
        super().__init__(message)


class RuntimeBridgeCancelled(RuntimeBridgeError):
    def __init__(self, message: str = "Runtime bridge request cancelled", *,
                 receipt: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(message, code="cancelled", receipt=receipt)


class RuntimeBridgeTimeout(RuntimeBridgeError):
    def __init__(self, message: str = "Runtime bridge request timed out", *,
                 receipt: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(message, code="timeout", receipt=receipt)


class RuntimeBridgeBackpressure(RuntimeBridgeError):
    def __init__(self, message: str = "Runtime bridge queue is full", *,
                 receipt: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(message, code="backpressure", receipt=receipt)


class RuntimeBridgeRecoveryUnknown(RuntimeBridgeError):
    def __init__(self, message: str = "Runtime call outcome is unknown; it was not replayed", *,
                 receipt: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(message, code="recovery_unknown", receipt=receipt)


class _RuntimeProcess:
    """One MCP process with request-id correlation and bounded response waits."""

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
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending: Dict[int, "queue.Queue[Optional[Dict[str, Any]]] "] = {}
        self._closed = threading.Event()
        self._reader = threading.Thread(
            target=self._read_stdout, name="loop-runtime-mcp-reader", daemon=True,
        )
        self._reader.start()
        try:
            self._initialize()
        except Exception:
            self.close()
            raise

    def _fail_pending(self) -> None:
        with self._state_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for waiter in pending:
            waiter.put_nowait(None)

    def _read_stdout(self) -> None:
        if self.process.stdout is None:
            self._closed.set()
            self._fail_pending()
            return
        for line in self.process.stdout:
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            request_id = response.get("id")
            if not isinstance(request_id, int):
                continue
            with self._state_lock:
                waiter = self._pending.pop(request_id, None)
            if waiter is not None:
                waiter.put_nowait(response)
        self._closed.set()
        self._fail_pending()

    def _request(self, method: str, params: Mapping[str, Any], *,
                 timeout: float = 10.0) -> Dict[str, Any]:
        if self.process.poll() is not None or self.process.stdin is None:
            raise RuntimeBridgeRecoveryUnknown("Runtime MCP process is not running")
        waiter: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=1)
        with self._state_lock:
            request_id = self._next_id
            self._next_id += 1
            self._pending[request_id] = waiter
        request = {"jsonrpc": "2.0", "id": request_id, "method": method,
                   "params": dict(params)}
        try:
            with self._write_lock:
                if self.process.poll() is not None or self.process.stdin is None:
                    raise RuntimeBridgeRecoveryUnknown("Runtime MCP process stopped before request")
                self.process.stdin.write(json.dumps(request) + "\n")
                self.process.stdin.flush()
            try:
                response = waiter.get(timeout=max(timeout, 0.001))
            except queue.Empty as exc:
                with self._state_lock:
                    self._pending.pop(request_id, None)
                raise RuntimeBridgeTimeout("Runtime MCP response timed out") from exc
        except (BrokenPipeError, OSError) as exc:
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise RuntimeBridgeRecoveryUnknown("Runtime MCP transport failed") from exc
        if response is None:
            raise RuntimeBridgeRecoveryUnknown("Runtime MCP closed before returning a response")
        if response.get("id") != request_id:
            raise RuntimeBridgeRecoveryUnknown("Runtime MCP response id does not match request")
        if "error" in response:
            raise RuntimeBridgeError(str(response["error"]), code="runtime_error")
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeBridgeError("Runtime MCP response omitted an object result")
        return result

    def _initialize(self) -> None:
        result = self._request("initialize", {
            "protocolVersion": RUNTIME_MCP_PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "simplicio-loop-hub", "version": RUNTIME_BRIDGE_SCHEMA},
        }, timeout=60.0)
        if result.get("protocolVersion") != RUNTIME_MCP_PROTOCOL:
            raise RuntimeBridgeError("Runtime MCP protocol version mismatch")
        if self.process.stdin is None:
            raise RuntimeBridgeRecoveryUnknown("Runtime MCP stdin is unavailable")
        with self._write_lock:
            self.process.stdin.write(json.dumps({
                "jsonrpc": "2.0", "method": "notifications/initialized",
            }) + "\n")
            self.process.stdin.flush()
        self.initialize_result = result
        self.tools_result = self._request("tools/list", {}, timeout=10.0)
        tools = self.tools_result.get("tools")
        # A malformed/partial capability response is represented as an empty
        # allow-list. Real effects then fail closed in ``_dispatch`` while
        # protocol/unit fixtures that only model ``initialize`` remain usable.
        if not isinstance(tools, list):
            tools = []
        self.available_tools = {
            str(item.get("name")) for item in tools
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        }

    def call_tool(self, name: str, arguments: Mapping[str, Any], *,
                  timeout: float = 10.0) -> Dict[str, Any]:
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
            raise RuntimeBridgeError(text, code="runtime_tool_error")
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeBridgeError("Runtime MCP tool returned non-JSON text") from exc
        if not isinstance(value, dict):
            raise RuntimeBridgeError("Runtime MCP tool returned a non-object payload")
        return value

    def close(self) -> None:
        self._closed.set()
        self._fail_pending()
        with self._write_lock:
            if self.process.poll() is None:
                self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)


class _WorkspaceSession:
    STATES = frozenset({"absent", "starting", "ready", "draining", "reconnecting", "failed", "closed"})

    def __init__(self, key: str, *, max_inflight: int, max_queue: int) -> None:
        self.key = key
        self.max_inflight = max_inflight
        self.max_queue = max_queue
        self.lifecycle_lock = threading.RLock()
        self.condition = threading.Condition()
        self.state = "absent"
        self.process: Optional[_RuntimeProcess] = None
        self.generation = 0
        self.reconnects = 0
        self.inflight = 0
        self.exclusive_active = False
        self._ticket = 0
        self._waiters: Deque[Tuple[int, bool, float]] = deque()
        self.wait_count = 0
        self.cancelled = 0
        self.timeouts = 0
        self.throttled = 0

    def acquire(self, *, exclusive: bool, deadline: float,
                cancel_event: Optional[threading.Event]) -> int:
        started = time.monotonic()
        with self.condition:
            if self.state in {"draining", "closed"}:
                raise RuntimeBridgeError("Runtime workspace session is closed", code="closed")
            if len(self._waiters) >= self.max_queue:
                self.throttled += 1
                raise RuntimeBridgeBackpressure(receipt=self.status())
            self._ticket += 1
            ticket = self._ticket
            self._waiters.append((ticket, exclusive, started))
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    self._waiters = deque(item for item in self._waiters if item[0] != ticket)
                    self.cancelled += 1
                    self.condition.notify_all()
                    raise RuntimeBridgeCancelled(receipt=self.status())
                if self.state in {"draining", "closed"}:
                    self._waiters = deque(item for item in self._waiters if item[0] != ticket)
                    self.condition.notify_all()
                    raise RuntimeBridgeError("Runtime workspace session is closed", code="closed")
                head = self._waiters[0] if self._waiters else None
                can_run = (
                    head is not None and head[0] == ticket and self.inflight < self.max_inflight
                    and (not exclusive or self.inflight == 0)
                    and (exclusive or not self.exclusive_active)
                )
                if can_run:
                    self._waiters.popleft()
                    self.inflight += 1
                    self.exclusive_active = self.exclusive_active or exclusive
                    return int((time.monotonic() - started) * 1000)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._waiters = deque(item for item in self._waiters if item[0] != ticket)
                    self.timeouts += 1
                    self.condition.notify_all()
                    raise RuntimeBridgeTimeout(receipt=self.status())
                self.wait_count += 1
                self.condition.wait(timeout=min(remaining, 0.05))

    def release(self, *, exclusive: bool) -> None:
        with self.condition:
            self.inflight = max(0, self.inflight - 1)
            if exclusive:
                self.exclusive_active = False
            self.condition.notify_all()

    def mark_recovery(self, process: Optional[_RuntimeProcess]) -> None:
        with self.lifecycle_lock:
            if self.process is process:
                self.process = None
                self.state = "reconnecting"
                self.reconnects += 1
        if process is not None:
            process.close()

    def status(self) -> Dict[str, Any]:
        with self.condition:
            return {
                "schema": "simplicio.runtime-bridge-session/v1",
                "workspace": self.key,
                "state": self.state,
                "generation": self.generation,
                "queue_depth": len(self._waiters),
                "inflight": self.inflight,
                "max_inflight": self.max_inflight,
                "max_queue": self.max_queue,
                "wait_count": self.wait_count,
                "cancelled": self.cancelled,
                "timeouts": self.timeouts,
                "reconnects": self.reconnects,
                "throttled": self.throttled,
            }


class RuntimeBridge:
    """Lazy Runtime owner with bounded, independent workspace sessions."""

    def __init__(self, binary: Optional[str] = None, *, max_inflight_per_workspace: int = 1,
                 max_queue_per_workspace: int = 32, max_global_inflight: int = 8,
                 max_global_queue: Optional[int] = None,
                 safe_read_tools: Optional[Set[str]] = None) -> None:
        if min(max_inflight_per_workspace, max_queue_per_workspace, max_global_inflight) < 1:
            raise ValueError("RuntimeBridge limits must be positive")
        self.binary = binary or os.environ.get("SIMPLICIO_RUNTIME_BIN") or "simplicio-runtime"
        self.max_inflight_per_workspace = max_inflight_per_workspace
        self.max_queue_per_workspace = max_queue_per_workspace
        self.max_global_inflight = max_global_inflight
        self.max_global_queue = max_global_queue if max_global_queue is not None else max_global_inflight * 4
        if self.max_global_queue < 1:
            raise ValueError("RuntimeBridge limits must be positive")
        self.safe_read_tools = frozenset(safe_read_tools or ())
        self._preflight_receipts: Dict[str, Dict[str, Any]] = {}
        self._sessions_lock = threading.RLock()
        self._sessions: Dict[str, _WorkspaceSession] = {}
        self._global_condition = threading.Condition()
        self._global_inflight = 0
        self._global_waiters = 0
        self._global_throttled = 0

    @staticmethod
    def _workspace_path(workspace: str) -> Path:
        if not workspace:
            raise RuntimeBridgeError("Runtime workspace is required")
        path = Path(workspace).expanduser().resolve()
        if not path.is_dir():
            raise RuntimeBridgeError("Runtime workspace does not exist")
        return path

    def _session_for_workspace(self, workspace_path: Path) -> _WorkspaceSession:
        key = str(workspace_path)
        with self._sessions_lock:
            return self._sessions.setdefault(
                key,
                _WorkspaceSession(key, max_inflight=self.max_inflight_per_workspace,
                                  max_queue=self.max_queue_per_workspace),
            )

    def _process_for_session(self, session: _WorkspaceSession,
                             workspace_path: Path) -> _RuntimeProcess:
        with session.lifecycle_lock:
            if session.process is not None and session.process.process.poll() is None:
                session.state = "ready"
                return session.process
            session.state = "starting"
            try:
                preflight_binary = None
                binary = self.binary or "simplicio"
                if _RuntimeProcess is _REAL_RUNTIME_PROCESS:
                    preflight_binary = resolve_and_probe_simplicio_binary(explicit=self.binary)
                    binary = preflight_binary.path
                process = _RuntimeProcess(binary, workspace_path)
                if preflight_binary is not None:
                    process.preflight_receipt = runtime_preflight(
                        binary=preflight_binary,
                        initialize_result=process.initialize_result,
                        tools_result=process.tools_result,
                        require_server_identity=True,
                    )
                    self._preflight_receipts[session.key] = process.preflight_receipt
            except Exception:
                session.state = "failed"
                raise
            session.process = process
            session.generation += 1
            session.state = "ready"
            return process

    def _process_for_workspace(self, workspace_path: Path) -> _RuntimeProcess:
        """Compatibility seam retained for Hub tests and controlled adapters."""
        return self._process_for_session(self._session_for_workspace(workspace_path), workspace_path)

    @staticmethod
    def _relative_cwd(cwd: str) -> Path:
        relative = Path(cwd)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeBridgeError("Runtime cwd must stay workspace-relative")
        return relative

    def _acquire_global(self, *, deadline: float,
                        cancel_event: Optional[threading.Event]) -> None:
        with self._global_condition:
            if self._global_waiters >= self.max_global_queue:
                self._global_throttled += 1
                raise RuntimeBridgeBackpressure(receipt=self.status())
            self._global_waiters += 1
            try:
                while self._global_inflight >= self.max_global_inflight:
                    if cancel_event is not None and cancel_event.is_set():
                        raise RuntimeBridgeCancelled()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._global_throttled += 1
                        raise RuntimeBridgeTimeout()
                    self._global_condition.wait(timeout=min(remaining, 0.05))
                self._global_inflight += 1
            finally:
                self._global_waiters = max(0, self._global_waiters - 1)

    def _release_global(self) -> None:
        with self._global_condition:
            self._global_inflight = max(0, self._global_inflight - 1)
            self._global_condition.notify_all()

    @staticmethod
    def _effect_transaction(*, tool: str, arguments: Mapping[str, Any],
                            relative_cwd: Path, idempotency_key: str,
                            timeout_ms: int,
                            canonical_plan: Optional[CanonicalPlan] = None) -> Dict[str, Any]:
        try:
            action = json.dumps({"tool": tool, "arguments": dict(arguments)},
                                sort_keys=True, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise RuntimeBridgeError("runtime_call arguments must be JSON-compatible") from exc
        transaction = {
            "schema": "simplicio.effect-transaction/v1", "executor": "simplicio-runtime",
            "request": {
                "schema": "simplicio.effect-request/v1", "capability": tool,
                "identity": {"session": "loop-hub-runtime", "turn": "runtime-bridge",
                              "tool_call": idempotency_key, "attempt": "1",
                              "transaction": idempotency_key},
                "authority": "loop-hub-runtime-bridge", "policy_receipt": "loop-hub-runtime-policy-v1",
                "idempotency_key": idempotency_key,
                "action_digest": "sha256:" + hashlib.sha256(action).hexdigest(),
                "write_set": ["repo:" + str(relative_cwd)], "preconditions": ["workspace-authorized"],
                "lease": {"id": "loop-hub-runtime-lease", "fence": 1},
                "deadline_ms": int(time.time() * 1000) + max(int(timeout_ms), 1),
                "cancellation": "safe_boundary_only", "validation_plan": "loop-hub-runtime-call-validation-v1",
                "rollback_plan": "runtime-call-boundary", "redaction_plan": "runtime-default-redaction",
            },
        }
        if canonical_plan is not None:
            transaction["canonical_plan"] = canonical_plan_metadata(canonical_plan)
        return transaction

    def _dispatch(self, workspace_path: Path, tool: str, arguments: Mapping[str, Any], *,
                  timeout_ms: int, cancel_event: Optional[threading.Event]) -> Dict[str, Any]:
        bounded_timeout_ms = min(max(int(timeout_ms), 1), 120_000)
        deadline = time.monotonic() + bounded_timeout_ms / 1000.0
        session = self._session_for_workspace(workspace_path)
        exclusive = tool not in self.safe_read_tools
        session.acquire(exclusive=exclusive, deadline=deadline, cancel_event=cancel_event)
        global_acquired = False
        process: Optional[_RuntimeProcess] = None
        try:
            self._acquire_global(deadline=deadline, cancel_event=cancel_event)
            global_acquired = True
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeBridgeCancelled(receipt=session.status())
            process = self._process_for_workspace(workspace_path)
            available = getattr(process, "available_tools", None)
            if available is not None and tool not in available:
                raise RuntimeBridgeError("Runtime MCP missing required tool: " + tool,
                                         code="capability_missing")
            if isinstance(self._preflight_receipts.get(str(workspace_path)), Mapping):
                enriched = dict(arguments)
                transaction = dict(enriched.get("__runtime_effect_transaction") or {})
                transaction["preflight"] = dict(self._preflight_receipts[str(workspace_path)])
                enriched["__runtime_effect_transaction"] = transaction
                arguments = enriched
            try:
                return process.call_tool(tool, arguments, timeout=max(deadline - time.monotonic(), 0.001))
            except RuntimeBridgeTimeout as exc:
                session.timeouts += 1
                session.mark_recovery(process)
                raise RuntimeBridgeRecoveryUnknown(
                    "Runtime MCP call timed out after dispatch; outcome is unknown and was not replayed",
                    receipt={**session.status(), "outcome": "unknown", "timeout": True},
                ) from exc
            except RuntimeBridgeRecoveryUnknown as exc:
                session.mark_recovery(process)
                raise RuntimeBridgeRecoveryUnknown(receipt={**session.status(), "outcome": "unknown"}) from exc
        finally:
            session.release(exclusive=exclusive)
            if global_acquired:
                self._release_global()

    def execute(self, workspace: str, argv: list[str], cwd: str = ".",
                env: Optional[Mapping[str, str]] = None, timeout_ms: int = 120_000,
                max_output_bytes: int = 4 * 1024 * 1024,
                idempotency_key: str = "", cancel_event: Optional[threading.Event] = None,
                canonical_plan: Optional[CanonicalPlan] = None) -> Dict[str, Any]:
        if not argv or not idempotency_key:
            raise RuntimeBridgeError("workspace, argv and idempotency_key are required")
        workspace_path = self._workspace_path(workspace)
        relative_cwd = self._relative_cwd(cwd)
        return self._dispatch(workspace_path, "simplicio_exec", {
            "repo": str(workspace_path), "cwd": str(relative_cwd), "argv": list(argv),
            "env": dict(env or {}), "timeout_ms": min(max(int(timeout_ms), 1), 120_000),
            "max_output_bytes": min(max(int(max_output_bytes), 1), 4 * 1024 * 1024),
            "idempotency_key": idempotency_key,
            "__runtime_effect_transaction": self._effect_transaction(
                tool="simplicio_exec", arguments={"argv": list(argv)}, relative_cwd=relative_cwd,
                idempotency_key=idempotency_key, timeout_ms=timeout_ms,
                canonical_plan=canonical_plan,
            ),
        }, timeout_ms=timeout_ms, cancel_event=cancel_event)

    def runtime_call(self, workspace: str, tool: str, arguments: Mapping[str, Any], *,
                     cwd: str = ".", timeout_ms: int = 120_000,
                     idempotency_key: str = "", cancel_event: Optional[threading.Event] = None,
                     canonical_plan: Optional[CanonicalPlan] = None) -> Dict[str, Any]:
        if not tool or not idempotency_key:
            raise RuntimeBridgeError("workspace, tool and idempotency_key are required")
        if not tool.startswith("simplicio_") or any(not (char.isalnum() or char in "_.-") for char in tool):
            raise RuntimeBridgeError("runtime_call tool must be a safe simplicio_ tool name")
        if not isinstance(arguments, Mapping):
            raise RuntimeBridgeError("runtime_call arguments must be an object")
        if "__runtime_effect_transaction" in arguments:
            raise RuntimeBridgeError("runtime_call transaction is bridge-owned")
        workspace_path = self._workspace_path(workspace)
        relative_cwd = self._relative_cwd(cwd)
        request_arguments = dict(arguments)
        request_arguments["__runtime_effect_transaction"] = self._effect_transaction(
            tool=tool, arguments=arguments, relative_cwd=relative_cwd,
            idempotency_key=idempotency_key, timeout_ms=timeout_ms,
            canonical_plan=canonical_plan,
        )
        return self._dispatch(workspace_path, tool, request_arguments,
                              timeout_ms=timeout_ms, cancel_event=cancel_event)

    def status(self, workspace: Optional[str] = None) -> Dict[str, Any]:
        with self._sessions_lock:
            sessions = list(self._sessions.values()) if workspace is None else [
                self._sessions.get(str(self._workspace_path(workspace))),
            ]
        entries = [session.status() for session in sessions if session is not None]
        with self._global_condition:
            return {
                "schema": "simplicio.runtime-bridge/v1", "sessions": entries,
                "global_inflight": self._global_inflight,
                "global_waiters": self._global_waiters,
                "max_global_inflight": self.max_global_inflight,
                "max_global_queue": self.max_global_queue,
                "global_throttled": self._global_throttled,
            }

    def close(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._preflight_receipts.clear()
        for session in sessions:
            with session.lifecycle_lock:
                session.state = "draining"
                process = session.process
                session.process = None
                session.state = "closed"
            with session.condition:
                session.condition.notify_all()
            if process is not None:
                process.close()


__all__ = [
    "RUNTIME_BRIDGE_SCHEMA", "RUNTIME_CALL_SCHEMA", "RuntimeBridge", "RuntimeBridgeError",
    "RuntimeBridgeCancelled", "RuntimeBridgeTimeout", "RuntimeBridgeBackpressure",
    "RuntimeBridgeRecoveryUnknown",
]


_REAL_RUNTIME_PROCESS = _RuntimeProcess
