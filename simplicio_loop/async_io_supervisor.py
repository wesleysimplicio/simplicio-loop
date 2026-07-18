"""Bounded async process supervision with cancellable leases.

This adapter composes the existing argv-only PythonProcessAdapter. It adds a
bounded semaphore, active lease registry, deterministic shutdown and expiry
recovery without creating polling workers.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import fields
from pathlib import Path
from typing import Any, Dict, Optional

from .process_supervisor import (
    ProcessLease,
    ProcessResult,
    ProcessSpec,
    PythonProcessAdapter,
)


class SupervisorClosed(RuntimeError):
    """The supervisor is draining or has already shut down."""


class DuplicateLease(RuntimeError):
    """A lease id is already active."""


class AsyncProcessSupervisor:
    """Run bounded async processes and recover/cancel their leases."""

    def __init__(
        self,
        *,
        adapter: Optional[PythonProcessAdapter] = None,
        max_concurrency: int = 4,
        state_path: Optional[str] = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self.adapter = adapter or PythonProcessAdapter()
        self.max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._leases: Dict[str, ProcessLease] = {}
        self._tasks: Dict[str, asyncio.Task[ProcessResult]] = {}
        self._draining = False
        self._state_path = Path(state_path) if state_path else None
        self._outcomes: Dict[str, Dict[str, Any]] = {}
        self._recovered_leases: list[str] = []
        self._load_state()

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._outcomes = {
            str(key): dict(value)
            for key, value in payload.get("outcomes", {}).items()
            if isinstance(value, dict)
        }
        now = time.time()
        for record in payload.get("leases", []):
            if not isinstance(record, dict):
                continue
            lease_id = str(record.get("lease_id", ""))
            # A new supervisor instance has no owner for leases written by the
            # previous instance. Treat them as abandoned even if their wall
            # clock TTL has not elapsed yet; this is the restart boundary.
            if lease_id and float(record.get("expires_at_wall", 0)) >= now:
                self._recovered_leases.append(lease_id)
        if self._recovered_leases:
            self._write_state()

    def _write_state(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "simplicio.async-process-supervisor-state/v1",
            "leases": [
                {
                    "lease_id": lease.lease_id,
                    "spec_hash": lease.spec_hash,
                    "expires_at_wall": time.time() + max(0, lease.expires_at - time.monotonic()),
                }
                for lease in self._leases.values()
            ],
            "outcomes": self._outcomes,
        }
        temporary = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self._state_path)

    @staticmethod
    def _result_from_dict(payload: Dict[str, Any]) -> ProcessResult:
        allowed = {field.name for field in fields(ProcessResult)}
        return ProcessResult(**{key: value for key, value in payload.items() if key in allowed})

    async def run(
        self,
        spec: ProcessSpec,
        *,
        lease: Optional[ProcessLease] = None,
    ) -> ProcessResult:
        if self._draining:
            raise SupervisorClosed("supervisor is draining")
        if spec.idempotency_key in self._outcomes:
            return self._result_from_dict(self._outcomes[spec.idempotency_key])
        process_lease = lease or ProcessLease(
            lease_id=spec.idempotency_key or "lease-" + uuid.uuid4().hex,
            spec_hash=spec.spec_hash,
        )
        if process_lease.lease_id in self._tasks:
            raise DuplicateLease(process_lease.lease_id)
        self._leases[process_lease.lease_id] = process_lease
        self._write_state()
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("run must execute in an asyncio task")
        self._tasks[process_lease.lease_id] = current
        try:
            async with self._semaphore:
                result = await self.adapter.run(spec, lease=process_lease)
                if spec.idempotency_key and result.returncode == 0 and not result.cancelled and not result.timed_out:
                    self._outcomes[spec.idempotency_key] = result.to_dict()
                return result
        finally:
            self._tasks.pop(process_lease.lease_id, None)
            self._leases.pop(process_lease.lease_id, None)
            self._write_state()

    async def recover_expired(self, *, now: Optional[float] = None) -> list[str]:
        expired = []
        for lease_id, lease in list(self._leases.items()):
            if lease.expired(now=now):
                expired.append(lease_id)
                task = self._tasks.get(lease_id)
                if task and task is not asyncio.current_task():
                    task.cancel()
        return expired

    async def shutdown(self) -> Dict[str, Any]:
        self._draining = True
        current = asyncio.current_task()
        tasks = [
            task for task in self._tasks.values()
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return self.status()

    def status(self) -> Dict[str, Any]:
        return {
            "schema": "simplicio.async-process-supervisor/v1",
            "draining": self._draining,
            "max_concurrency": self.max_concurrency,
            "active_leases": len(self._leases),
            "active_tasks": len(self._tasks),
            "semaphore_available": getattr(self._semaphore, "_value", None),
            "lease_ids": sorted(self._leases),
            "recovered_leases": list(self._recovered_leases),
            "persisted_outcomes": len(self._outcomes),
        }
