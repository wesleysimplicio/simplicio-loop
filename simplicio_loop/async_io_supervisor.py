"""Bounded async process supervision with cancellable leases.

This adapter composes the existing argv-only PythonProcessAdapter. It adds a
bounded semaphore, active lease registry, deterministic shutdown and expiry
recovery without creating polling workers.
"""

from __future__ import annotations

import asyncio
import uuid
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
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self.adapter = adapter or PythonProcessAdapter()
        self.max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._leases: Dict[str, ProcessLease] = {}
        self._tasks: Dict[str, asyncio.Task[ProcessResult]] = {}
        self._draining = False

    async def run(
        self,
        spec: ProcessSpec,
        *,
        lease: Optional[ProcessLease] = None,
    ) -> ProcessResult:
        if self._draining:
            raise SupervisorClosed("supervisor is draining")
        process_lease = lease or ProcessLease(
            lease_id=spec.idempotency_key or "lease-" + uuid.uuid4().hex,
            spec_hash=spec.spec_hash,
        )
        if process_lease.lease_id in self._tasks:
            raise DuplicateLease(process_lease.lease_id)
        self._leases[process_lease.lease_id] = process_lease
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("run must execute in an asyncio task")
        self._tasks[process_lease.lease_id] = current
        try:
            async with self._semaphore:
                return await self.adapter.run(spec, lease=process_lease)
        finally:
            self._tasks.pop(process_lease.lease_id, None)
            self._leases.pop(process_lease.lease_id, None)

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
        }
