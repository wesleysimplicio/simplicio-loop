"""Durable, cancellable process execution owned exclusively by the Hub."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .hub_governor import RESOURCE_NAMES, ResourceGovernor, ResourceRequest, ResourceThrottled
from .mapper_operations import MapperOperationsAdapter
from .process_supervisor import ProcessLease, ProcessResult, ProcessSpec, PythonProcessAdapter


NAMESPACE = "hub-agent/v1"
CAPABILITY = "hub-agent-process/v1"
TERMINAL = frozenset({"completed", "failed", "cancelled", "timed_out", "recovery_unknown"})
EVENT_SCHEMA = "simplicio.loop-hub-agent-execution-event/v1"
JOURNAL_PREFIX = "simplicio.loop.hub-agent-executor:"


class HubAgentError(RuntimeError):
    pass


class StaleFence(HubAgentError):
    pass


class _JournalConflict(HubAgentError):
    """The projected execution changed before this mutation committed."""


class HubAgentExecutor:
    """A single background event loop with a durable, fenced lifecycle journal."""

    def __init__(
        self, path: str, governor: ResourceGovernor, *, max_concurrency: int = 4,
        adapter: Optional[PythonProcessAdapter] = None,
        operations: Any | None = None,
    ) -> None:
        self.path = str(Path(path).expanduser().absolute())
        self.governor = governor
        self.max_concurrency = max_concurrency
        self.adapter = adapter or PythonProcessAdapter()
        self.epoch = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._operations = operations or MapperOperationsAdapter(self.path)
        self._journal_id = JOURNAL_PREFIX + self.path
        self._operations.initialize()
        self._recover_previous_epoch()
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._tasks: Dict[str, asyncio.Task[None]] = {}
        self._processes: Dict[str, asyncio.subprocess.Process] = {}
        self._leases: Dict[str, Any] = {}
        self._thread = threading.Thread(target=self._run_loop, name="hub-agent-executor", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    @staticmethod
    def _last_seq(replay: Mapping[str, Any]) -> int:
        events = replay.get("events") or []
        if events:
            return int(events[-1]["seq"])
        compaction = replay.get("compaction")
        return int(compaction["through_seq"]) if compaction else 0

    def _replay(self) -> dict[str, Any]:
        replay = self._operations.replay(self._journal_id)
        if not replay.get("valid", False):
            raise HubAgentError("hub agent journal is invalid")
        return replay

    @staticmethod
    def _state(replay: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        state: dict[str, dict[str, Any]] = {}
        for journal_event in replay.get("events", []):
            payload = journal_event.get("payload")
            if not isinstance(payload, Mapping) or payload.get("schema") != EVENT_SCHEMA:
                raise HubAgentError("hub agent journal contains an unknown event")
            if payload.get("operation") != "execution":
                raise HubAgentError("hub agent journal contains an unknown operation")
            execution = dict(payload.get("execution") or {})
            handle = str(execution.get("handle") or "")
            if not handle:
                raise HubAgentError("hub agent journal contains an execution without a handle")
            state[handle] = execution
        return state

    @staticmethod
    def _is_conflict(error: BaseException) -> bool:
        return "JOURNAL_CONFLICT" in str(error) or getattr(error, "reason_code", "") == "JOURNAL_CONFLICT"

    def _append(self, replay: Mapping[str, Any], event_type: str, execution: Mapping[str, Any]) -> None:
        try:
            self._operations.append_event(
                self._journal_id,
                event_type,
                {"schema": EVENT_SCHEMA, "operation": "execution", "execution": dict(execution)},
                expected_seq=self._last_seq(replay),
            )
        except Exception as error:
            if self._is_conflict(error):
                raise _JournalConflict("hub agent journal changed concurrently") from error
            raise HubAgentError("hub agent journal append failed: " + str(error)) from error

    def _recover_previous_epoch(self) -> None:
        with self._lock:
            for _ in range(32):
                replay = self._replay()
                state = self._state(replay)
                candidates = [
                    execution for execution in state.values()
                    if execution.get("namespace") == NAMESPACE
                    and execution.get("state") in {"claimed", "running", "cancelling"}
                ]
                if not candidates:
                    return
                try:
                    for execution in candidates:
                        now = time.time()
                        recovered = {
                            **execution,
                            "state": "recovery_unknown",
                            "updated_at": now,
                            "heartbeat_at": now,
                            "receipt": self._receipt("recovery_unknown", now, reason="previous_epoch"),
                        }
                        self._append(replay, "hub-agent.recovery-unknown", recovered)
                        replay = self._replay()
                    return
                except _JournalConflict:
                    continue
        raise HubAgentError("hub agent recovery remained contended")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._ready.set()
        self._loop.run_forever()

    @staticmethod
    def _receipt(state: str, at: float, **extra: Any) -> Dict[str, Any]:
        value = {
            "schema": "simplicio.hub-agent-execution-receipt/v1", "state": state,
            "recorded_at": at, "cpu_seconds": None, "peak_memory_bytes": None,
            "metrics_reason": "unmeasured",
        }
        value.update(extra)
        return value

    def claim(self, spec: ProcessSpec, request: ResourceRequest, *, idempotency_key: str) -> Dict[str, Any]:
        if not idempotency_key:
            raise HubAgentError("idempotency_key is required")
        with self._lock:
            for _ in range(32):
                replay = self._replay()
                state = self._state(replay)
                existing = next(
                    (execution for execution in state.values()
                     if execution.get("idempotency_key") == idempotency_key),
                    None,
                )
                if existing is not None:
                    if existing["spec"]["spec_hash"] != spec.spec_hash:
                        raise HubAgentError("idempotency key conflicts with ProcessSpec")
                    return self._view(existing)
                now = time.time()
                handle = "ha-" + uuid.uuid4().hex
                try:
                    lease = self.governor.admit(NAMESPACE, handle, request, queue=NAMESPACE)
                except ResourceThrottled as exc:
                    raise HubAgentError("backpressure: " + str(exc)) from exc
                execution = {
                    "handle": handle,
                    "namespace": NAMESPACE,
                    "idempotency_key": idempotency_key,
                    "spec": spec.to_dict(),
                    "request": request.as_dict(),
                    "priority": spec.priority,
                    "state": "claimed",
                    "fence": 1,
                    "epoch": self.epoch,
                    "result": None,
                    "receipt": None,
                    "created_at": now,
                    "updated_at": now,
                    "heartbeat_at": now,
                }
                try:
                    self._append(replay, "hub-agent.claimed", execution)
                    self._leases[handle] = lease
                    return self._view(execution)
                except _JournalConflict:
                    self.governor.release(lease)
                    continue
                except BaseException:
                    self.governor.release(lease)
                    raise
        raise HubAgentError("hub agent claim remained contended")

    def send(self, handle: str, fence: int) -> Dict[str, Any]:
        with self._lock:
            row = self._checked(handle, fence)
            if row["state"] in TERMINAL or row["state"] == "running":
                return self._view(row)
            if row["state"] != "claimed":
                raise HubAgentError("execution is not sendable")
            now = time.time()
            updated = {**row, "state": "running", "fence": int(fence) + 1,
                       "updated_at": now, "heartbeat_at": now}
            self._append(self._replay(), "hub-agent.started", updated)
            self._loop.call_soon_threadsafe(self._spawn_task, handle, int(fence) + 1)
            return self._view(updated)

    def _spawn_task(self, handle: str, fence: int) -> None:
        task = self._loop.create_task(self._execute(handle, fence))
        self._tasks[handle] = task

    async def _execute(self, handle: str, fence: int) -> None:
        row = self._row(handle)
        raw = row["spec"]
        spec = ProcessSpec(
            tuple(raw["argv"]), cwd=raw.get("cwd"), cwd_allowlist=tuple(raw.get("cwd_allowlist", ())),
            env=raw.get("env", {}), env_allowlist=tuple(raw.get("env_allowlist", ())),
            timeout_seconds=raw.get("timeout_seconds"), max_output_bytes=raw.get("max_output_bytes", 65536),
            priority=raw.get("priority", 0), idempotency_key=raw.get("idempotency_key", ""),
        )
        lease = ProcessLease(handle, spec.spec_hash)
        adapter = self.adapter

        def spawned(process: asyncio.subprocess.Process) -> None:
            self._processes[handle] = process

        try:
            async with self._semaphore:
                heartbeat = self._loop.create_task(self._heartbeat(handle, fence))
                try:
                    result = await adapter.run(spec, lease=lease, on_spawned=spawned)
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
            state = "timed_out" if result.timed_out else ("cancelled" if result.cancelled else
                    ("completed" if result.returncode == 0 else "failed"))
        except MemoryError:
            result = ProcessResult(None, error_code="oom", lease_id=handle)
            state = "failed"
        finally:
            self._processes.pop(handle, None)
            self._tasks.pop(handle, None)
        now = time.time()
        with self._lock:
            current = self._row(handle)
            receipt = self._receipt(state, now, epoch=self.epoch, handle=handle, fence=current["fence"])
            updated = {**current, "state": state, "result": result.to_dict(), "receipt": receipt,
                       "updated_at": now, "heartbeat_at": now}
            self._append(self._replay(), "hub-agent.terminal", updated)
            resource_lease = self._leases.pop(handle, None)
            if resource_lease is not None:
                self.governor.release(resource_lease)

    async def _heartbeat(self, handle: str, fence: int) -> None:
        while True:
            await asyncio.sleep(0.1)
            with self._lock:
                replay = self._replay()
                current = self._state(replay).get(handle)
                if current is None or current["fence"] != fence or current["state"] != "running":
                    return
                now = time.time()
                try:
                    self._append(replay, "hub-agent.heartbeat", {
                        **current, "updated_at": now, "heartbeat_at": now,
                    })
                except _JournalConflict:
                    continue

    def cancel(self, handle: str, fence: int) -> Dict[str, Any]:
        with self._lock:
            row = self._checked(handle, fence)
            if row["state"] in TERMINAL:
                return self._view(row)
            now = time.time()
            updated = {**row, "state": "cancelling", "fence": int(fence) + 1, "updated_at": now}
            self._append(self._replay(), "hub-agent.cancelling", updated)
            task = self._tasks.get(handle)
            if task:
                self._loop.call_soon_threadsafe(task.cancel)
            return self._view(updated)

    def _row(self, handle: str) -> dict[str, Any]:
        with self._lock:
            row = self._state(self._replay()).get(handle)
        if row is None:
            raise HubAgentError("unknown handle")
        return row

    def _checked(self, handle: str, fence: int) -> dict[str, Any]:
        row = self._row(handle)
        if int(row["fence"]) != int(fence):
            raise StaleFence("stale fence")
        return row

    def status(self, handle: str) -> Dict[str, Any]:
        return self._view(self._row(handle))

    def collect(self, handle: str) -> Dict[str, Any]:
        value = self.status(handle)
        if value["state"] not in TERMINAL:
            raise HubAgentError("execution is not terminal")
        return value

    @staticmethod
    def _view(row: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "schema": "simplicio.hub-agent-execution/v1", "capability": CAPABILITY,
            "namespace": row["namespace"], "handle": row["handle"], "state": row["state"],
            "fence": row["fence"], "epoch": row["epoch"], "priority": row["priority"],
            "heartbeat_at": row["heartbeat_at"],
            "result": row.get("result"), "receipt": row.get("receipt"),
        }

    def close(self) -> None:
        abandoned = list(self._leases)
        for task in list(self._tasks.values()):
            self._loop.call_soon_threadsafe(task.cancel)
        if self._tasks:
            deadline = time.time() + 2
            while self._tasks and time.time() < deadline:
                time.sleep(0.01)
        now = time.time()
        with self._lock:
            for handle in abandoned:
                current = self._row(handle)
                recovered = {
                    **current,
                    "state": "recovery_unknown",
                    "updated_at": now,
                    "heartbeat_at": now,
                    "receipt": self._receipt("recovery_unknown", now, reason="shutdown"),
                }
                self._append(self._replay(), "hub-agent.recovery-unknown", recovered)
            for lease in self._leases.values():
                self.governor.release(lease)
            self._leases.clear()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(2)


def parse_request(raw: Any) -> ResourceRequest:
    if not isinstance(raw, dict) or set(raw) - set(RESOURCE_NAMES):
        raise HubAgentError("request must contain only known resource fields")
    return ResourceRequest(**{name: int(raw.get(name, 0)) for name in RESOURCE_NAMES})
