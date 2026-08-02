"""MapperStore-backed queue facade for Loop issue #1026.

This is the queue-facing composition layer. It delegates task, lease, fence,
receipt, checkpoint, and reclaim semantics to ``MapperOperationsAdapter`` and
never imports sqlite3 or falls back to ``LocalTaskQueue``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .mapper_operations import MapperOperationsAdapter, OperationLease

MAPPER_QUEUE_SCHEMA = "simplicio.loop.mapper-queue/v1"


class MapperQueue:
    """Explicit MapperStore queue surface; construction is side-effect free."""

    schema = MAPPER_QUEUE_SCHEMA

    def __init__(
        self,
        database: str | Path,
        *,
        adapter: MapperOperationsAdapter | None = None,
        auto_create: bool = False,
    ) -> None:
        self.database = Path(database).expanduser().absolute()
        self.operations = adapter or MapperOperationsAdapter(
            self.database, auto_create=auto_create
        )

    def initialize(self) -> dict[str, Any]:
        """Explicitly initialize MapperStore; no implicit local schema exists."""
        return self.operations.initialize()

    def capabilities(self) -> dict[str, Any]:
        return self.operations.capabilities()

    def register_slot(self, slot_id: str, capacity: int) -> dict[str, Any]:
        return self.operations.register_slot(slot_id, capacity)

    def submit(
        self,
        task_id: str,
        payload: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str,
        priority: int = 0,
    ) -> dict[str, Any]:
        task_id = str(task_id).strip()
        if not task_id:
            raise ValueError("task_id is required")
        if not str(idempotency_key).strip():
            raise ValueError("idempotency_key is required")
        return self.operations.enqueue(
            task_id,
            dict(payload or {}),
            idempotency_key=str(idempotency_key),
            priority=int(priority),
        )

    def claim_next(
        self,
        worker_id: str,
        *,
        slot_id: str = "default",
        lease_seconds: float = 30.0,
    ) -> OperationLease | None:
        return self.operations.claim_next(
            worker_id, slot_id=slot_id, lease_seconds=lease_seconds
        )

    def heartbeat(
        self, lease: OperationLease, *, lease_seconds: float = 30.0
    ) -> OperationLease:
        return self.operations.heartbeat(lease, lease_seconds=lease_seconds)

    def release(self, lease: OperationLease) -> dict[str, Any]:
        return self.operations.release(lease)

    def complete(
        self,
        lease: OperationLease,
        *,
        receipt: Mapping[str, Any],
        status: str = "completed",
    ) -> dict[str, Any]:
        if not receipt:
            raise ValueError("receipt is required for completion")
        return self.operations.complete(lease, receipt=receipt, status=status)

    def cancel(self, task_id: str) -> dict[str, Any]:
        return self.operations.cancel(task_id)

    def reclaim_expired(self) -> dict[str, Any]:
        return self.operations.reclaim_expired()

    def checkpoint(
        self,
        lease: OperationLease,
        cursor: int,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self.operations.checkpoint(lease, cursor, payload)

    def status(self, task_id: str | None = None) -> dict[str, Any]:
        return self.operations.status(task_id)


__all__ = ["MAPPER_QUEUE_SCHEMA", "MapperQueue"]
