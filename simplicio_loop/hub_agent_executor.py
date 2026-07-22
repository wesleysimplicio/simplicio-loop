"""Durable, cancellable process execution owned exclusively by the Hub."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from .hub_governor import RESOURCE_NAMES, ResourceGovernor, ResourceRequest, ResourceThrottled
from .process_supervisor import ProcessLease, ProcessResult, ProcessSpec, PythonProcessAdapter


NAMESPACE = "hub-agent/v1"
CAPABILITY = "hub-agent-process/v1"
TERMINAL = frozenset({"completed", "failed", "cancelled", "timed_out", "recovery_unknown"})


class HubAgentError(RuntimeError):
    pass


class StaleFence(HubAgentError):
    pass


class HubAgentExecutor:
    """A single background event loop with a durable, fenced lifecycle store."""

    def __init__(
        self, path: str, governor: ResourceGovernor, *, max_concurrency: int = 4,
        adapter: Optional[PythonProcessAdapter] = None,
    ) -> None:
        self.path = str(Path(path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.governor = governor
        self.max_concurrency = max_concurrency
        self.adapter = adapter or PythonProcessAdapter()
        self.epoch = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS hub_agent_executions(
              handle TEXT PRIMARY KEY, namespace TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
              spec TEXT NOT NULL, request TEXT NOT NULL, priority INTEGER NOT NULL, state TEXT NOT NULL,
              fence INTEGER NOT NULL, epoch TEXT NOT NULL, result TEXT, receipt TEXT,
              created_at REAL NOT NULL, updated_at REAL NOT NULL, heartbeat_at REAL NOT NULL)
        """)
        now = time.time()
        self._db.execute(
            "UPDATE hub_agent_executions SET state='recovery_unknown',updated_at=?,receipt=? "
            "WHERE namespace=? AND state IN ('claimed','running','cancelling')",
            (now, json.dumps(self._receipt("recovery_unknown", now, reason="previous_epoch")), NAMESPACE),
        )
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._tasks: Dict[str, asyncio.Task[None]] = {}
        self._processes: Dict[str, asyncio.subprocess.Process] = {}
        self._leases: Dict[str, Any] = {}
        self._thread = threading.Thread(target=self._run_loop, name="hub-agent-executor", daemon=True)
        self._thread.start()
        self._ready.wait(5)

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
        now = time.time()
        with self._lock:
            existing = self._db.execute(
                "SELECT * FROM hub_agent_executions WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing:
                if json.loads(existing["spec"])["spec_hash"] != spec.spec_hash:
                    raise HubAgentError("idempotency key conflicts with ProcessSpec")
                return self._view(existing)
            handle = "ha-" + uuid.uuid4().hex
            try:
                lease = self.governor.admit(NAMESPACE, handle, request, queue=NAMESPACE)
            except ResourceThrottled as exc:
                raise HubAgentError("backpressure: " + str(exc)) from exc
            self._leases[handle] = lease
            self._db.execute(
                "INSERT INTO hub_agent_executions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (handle, NAMESPACE, idempotency_key, json.dumps(spec.to_dict(), sort_keys=True),
                 json.dumps(request.as_dict(), sort_keys=True), spec.priority, "claimed", 1,
                 self.epoch, None, None, now, now, now),
            )
            return self.status(handle)

    def send(self, handle: str, fence: int) -> Dict[str, Any]:
        with self._lock:
            row = self._checked(handle, fence)
            if row["state"] in TERMINAL or row["state"] == "running":
                return self._view(row)
            if row["state"] != "claimed":
                raise HubAgentError("execution is not sendable")
            next_fence = fence + 1
            self._db.execute(
                "UPDATE hub_agent_executions SET state='running',fence=?,updated_at=?,heartbeat_at=? WHERE handle=?",
                (next_fence, time.time(), time.time(), handle),
            )
            self._loop.call_soon_threadsafe(self._spawn_task, handle, next_fence)
            return self.status(handle)

    def _spawn_task(self, handle: str, fence: int) -> None:
        task = self._loop.create_task(self._execute(handle, fence))
        self._tasks[handle] = task

    async def _execute(self, handle: str, fence: int) -> None:
        row = self._row(handle)
        raw = json.loads(row["spec"])
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
        receipt = self._receipt(state, now, epoch=self.epoch, handle=handle, fence=fence)
        with self._lock:
            current_fence = int(self._row(handle)["fence"])
            receipt["fence"] = current_fence
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute(
                "UPDATE hub_agent_executions SET state=?,result=?,receipt=?,updated_at=?,heartbeat_at=? "
                "WHERE handle=? AND fence=?",
                (state, json.dumps(result.to_dict(), sort_keys=True), json.dumps(receipt, sort_keys=True),
                 now, now, handle, current_fence),
            )
            self._db.execute("COMMIT")
            resource_lease = self._leases.pop(handle, None)
            if resource_lease is not None:
                self.governor.release(resource_lease)

    async def _heartbeat(self, handle: str, fence: int) -> None:
        while True:
            await asyncio.sleep(0.1)
            with self._lock:
                self._db.execute(
                    "UPDATE hub_agent_executions SET heartbeat_at=? WHERE handle=? AND fence=? AND state='running'",
                    (time.time(), handle, fence),
                )

    def cancel(self, handle: str, fence: int) -> Dict[str, Any]:
        with self._lock:
            row = self._checked(handle, fence)
            if row["state"] in TERMINAL:
                return self._view(row)
            next_fence = fence + 1
            self._db.execute(
                "UPDATE hub_agent_executions SET state='cancelling',fence=?,updated_at=? WHERE handle=?",
                (next_fence, time.time(), handle),
            )
            task = self._tasks.get(handle)
            if task:
                self._loop.call_soon_threadsafe(task.cancel)
            return self.status(handle)

    def _row(self, handle: str) -> sqlite3.Row:
        with self._lock:
            row = self._db.execute("SELECT * FROM hub_agent_executions WHERE handle=?", (handle,)).fetchone()
        if row is None:
            raise HubAgentError("unknown handle")
        return row

    def _checked(self, handle: str, fence: int) -> sqlite3.Row:
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
    def _view(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "schema": "simplicio.hub-agent-execution/v1", "capability": CAPABILITY,
            "namespace": row["namespace"], "handle": row["handle"], "state": row["state"],
            "fence": row["fence"], "epoch": row["epoch"], "priority": row["priority"],
            "heartbeat_at": row["heartbeat_at"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "receipt": json.loads(row["receipt"]) if row["receipt"] else None,
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
                self._db.execute(
                    "UPDATE hub_agent_executions SET state='recovery_unknown',updated_at=?,receipt=? "
                    "WHERE handle=?",
                    (now, json.dumps(self._receipt("recovery_unknown", now, reason="shutdown")), handle),
                )
            for lease in self._leases.values():
                self.governor.release(lease)
            self._leases.clear()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(2)
        self._db.close()


def parse_request(raw: Any) -> ResourceRequest:
    if not isinstance(raw, dict) or set(raw) - set(RESOURCE_NAMES):
        raise HubAgentError("request must contain only known resource fields")
    return ResourceRequest(**{name: int(raw.get(name, 0)) for name in RESOURCE_NAMES})
