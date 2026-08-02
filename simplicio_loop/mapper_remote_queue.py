"""RemoteQueue-compatible MapperStore operations adapter.

The worker protocol remains owned by Loop while task, lease, slot and receipt
persistence is delegated to MapperStore.  This module deliberately contains no
SQLite import and has no local fallback; callers must initialize or hand off
the canonical operations store explicitly.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .mapper_operations import MapperOperationsAdapter, OperationLease
from .remote_queue import Lease, QueueConflict, QueueUnavailable

MAPPER_QUEUE_SCHEMA = "simplicio.loop.mapper-remote-queue/v1"
MAPPER_RECEIPT_SCHEMA = "simplicio.mapper-store.queue-receipt/v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def build_mapper_completion_receipt(
    *,
    task_id: str,
    agent_id: str,
    fencing_token: str,
    receipt_ref: str,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a receipt bound to Mapper's opaque string fencing token."""
    body: dict[str, Any] = {
        "schema": MAPPER_RECEIPT_SCHEMA,
        "task_id": str(task_id),
        "agent_id": str(agent_id),
        "fencing_token": str(fencing_token),
        "receipt_ref": str(receipt_ref),
    }
    if detail:
        body["detail"] = json.loads(json.dumps(dict(detail), default=str))
    body["receipt_sha"] = _digest(body)
    return body


def _operation_to_lease(value: OperationLease, idempotency_key: str) -> Lease:
    if value.expires_at is None:
        raise QueueUnavailable("MapperStore returned a lease without expiry")
    return Lease(
        task_id=value.task_id,
        agent_id=value.worker_id,
        lease_id=value.lease_id,
        # Mapper uses opaque string fences; preserve them byte-for-byte even
        # though the legacy RemoteQueue dataclass annotated its token as int.
        fencing_token=value.fence_token,  # type: ignore[arg-type]
        expires_at=float(value.expires_at),
        idempotency_key=idempotency_key,
        identity={"agent_id": value.worker_id},
        capabilities=(),
        cancelled=value.cancelled,
        attempt_id=value.attempt_id,
    )


class MapperRemoteQueue:
    """Explicit ``RemoteQueue`` facade backed only by MapperStore operations."""

    schema = MAPPER_QUEUE_SCHEMA

    def __init__(
        self,
        database: str | Path,
        *,
        adapter: MapperOperationsAdapter | None = None,
        auto_create: bool = False,
        slot_id: str = "default",
    ) -> None:
        self.database = Path(database).expanduser().absolute()
        self.slot_id = str(slot_id).strip()
        if not self.slot_id:
            raise ValueError("slot_id is required")
        self.operations = adapter or MapperOperationsAdapter(
            self.database, auto_create=auto_create
        )

    def initialize(self) -> dict[str, Any]:
        return self.operations.initialize()

    def build_completion_receipt(
        self,
        *,
        task_id: str,
        agent_id: str,
        fencing_token: str,
        receipt_ref: str,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return build_mapper_completion_receipt(
            task_id=task_id,
            agent_id=agent_id,
            fencing_token=str(fencing_token),
            receipt_ref=receipt_ref,
            detail=extra,
        )

    def pull(
        self,
        agent_id: str,
        *,
        capabilities: Optional[Sequence[str]] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        allowed = {str(value).strip() for value in (capabilities or ()) if str(value).strip()}
        result = self.operations.list_ready(limit=limit)
        tasks: list[dict[str, Any]] = []
        for item in result.get("tasks", ()):
            payload = dict(item.get("payload") or {})
            required = sorted(
                {str(value).strip() for value in payload.get("required_capabilities", ()) if str(value).strip()}
            )
            if required and not set(required).issubset(allowed):
                continue
            tasks.append(
                {
                    "task_id": str(item["task_id"]),
                    "status": "ready",
                    "required_capabilities": required,
                    "depends_on": sorted(
                        {str(value).strip() for value in payload.get("depends_on", ()) if str(value).strip()}
                    ),
                    "updated_at": item.get("updated_at"),
                }
            )
        return tasks

    def enqueue(
        self,
        task_id: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        idempotency_key: str | None = None,
        priority: int = 0,
    ) -> None:
        key = str(idempotency_key or f"loop:task:{task_id}").strip()
        self.operations.enqueue(
            str(task_id), dict(payload or {}), idempotency_key=key, priority=int(priority)
        )

    def claim(
        self,
        task_id: str,
        agent_id: str,
        *,
        idempotency_key: str,
        ttl: float = 60.0,
        identity: Optional[Mapping[str, Any]] = None,
        capabilities: Optional[Sequence[str]] = None,
    ) -> Lease:
        if identity is not None and str(identity.get("agent_id") or "") != str(agent_id):
            raise QueueConflict("agent_id does not match distributed identity")
        operation = self.operations.claim_task(
            str(task_id), str(agent_id), slot_id=self.slot_id, lease_seconds=ttl
        )
        if operation is None:
            raise QueueConflict("task is not ready or is already leased")
        lease = _operation_to_lease(operation, str(idempotency_key))
        return Lease(
            **{**lease.__dict__, "identity": dict(identity) if identity is not None else lease.identity,
               "capabilities": tuple(capabilities or ())}
        )

    def heartbeat(self, lease: Lease, *, ttl: float = 60.0) -> Lease:
        operation = self.operations.heartbeat(
            OperationLease(
                lease.task_id,
                lease.attempt_id,
                str(lease.fencing_token),
                lease.lease_id,
                lease.agent_id,
                {},
                lease.expires_at,
                lease.cancelled,
            ),
            lease_seconds=ttl,
        )
        return Lease(
            **{**lease.__dict__, "expires_at": float(operation.expires_at or 0),
               "cancelled": operation.cancelled}
        )

    def complete(
        self,
        lease: Lease,
        *,
        receipt_ref: str,
        receipt: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not receipt:
            raise QueueConflict("RECEIPT_REQUIRED")
        supplied = dict(receipt)
        if str(supplied.get("task_id") or "") != lease.task_id:
            raise QueueConflict("receipt task_id does not match the active lease")
        if str(supplied.get("agent_id") or "") != lease.agent_id:
            raise QueueConflict("receipt agent_id does not match the active lease")
        if str(supplied.get("fencing_token") or "") != str(lease.fencing_token):
            raise QueueConflict("receipt fencing_token does not match the active lease")
        if str(supplied.get("receipt_ref") or "") != str(receipt_ref):
            raise QueueConflict("receipt_ref does not match the completion request")
        operation = self.operations.complete(
            OperationLease(
                lease.task_id,
                lease.attempt_id,
                str(lease.fencing_token),
                lease.lease_id,
                lease.agent_id,
                {},
                lease.expires_at,
                lease.cancelled,
            ),
            receipt=supplied,
        )
        return {"schema": MAPPER_QUEUE_SCHEMA, "task_id": lease.task_id, **operation}

    def assert_active(self, lease: Lease) -> None:
        self.operations.assert_active(
            OperationLease(
                lease.task_id,
                lease.attempt_id,
                str(lease.fencing_token),
                lease.lease_id,
                lease.agent_id,
                {},
                lease.expires_at,
                lease.cancelled,
            )
        )

    def request_cancel(self, task_id: str, *, reason: str = "cancelled") -> Dict[str, Any]:
        return self.operations.request_cancel(str(task_id), reason=reason)

    def release(self, lease: Lease, *, reason: str = "handoff") -> Dict[str, Any]:
        return self.operations.release(
            OperationLease(
                lease.task_id,
                lease.attempt_id,
                str(lease.fencing_token),
                lease.lease_id,
                lease.agent_id,
                {},
                lease.expires_at,
                lease.cancelled,
            )
        )

    def task(self, task_id: str) -> Dict[str, Any]:
        return self.operations.status(str(task_id))


__all__ = [
    "MAPPER_QUEUE_SCHEMA",
    "MAPPER_RECEIPT_SCHEMA",
    "MapperRemoteQueue",
    "build_mapper_completion_receipt",
]
