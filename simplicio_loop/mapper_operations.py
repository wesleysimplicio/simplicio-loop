"""Loop's operations boundary for the installed MapperStore API (#1026).

The adapter deliberately does not open SQLite, inspect Mapper tables, or fall
back to a Loop-owned queue.  MapperStore owns persistence and atomic
operations; Loop owns worker policy and execution.  Importing this module is
side-effect free.  ``initialize`` is the explicit state-changing operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .remote_queue import QueueConflict, QueueUnavailable


class MapperOperationsError(RuntimeError):
    """A MapperStore operation failed without an allowed local fallback."""

    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = str(reason_code)
        super().__init__(
            f"{self.reason_code}: {detail}" if detail else self.reason_code
        )


@dataclass(frozen=True)
class OperationLease:
    """Lease returned by MapperStore, retaining its opaque fencing token."""

    task_id: str
    attempt_id: str
    fence_token: str
    lease_id: str
    worker_id: str
    payload: dict[str, Any]
    expires_at: float | None = None
    cancelled: bool = False


_CONFLICT_CODES = {
    "STALE_FENCE",
    "LEASE_NOT_ACTIVE",
    "LEASE_EXPIRED",
    "SLOT_CAPACITY",
    "IDEMPOTENCY_CONFLICT",
    "RECEIPT_REQUIRED",
    "EFFECT_RECONCILIATION_PENDING",
}
_NOT_FOUND_CODES = {"TASK_NOT_FOUND", "SLOT_NOT_FOUND", "EFFECT_NOT_FOUND"}


def _load_store_error_type() -> type[BaseException] | tuple[type[BaseException], ...]:
    try:
        from simplicio_mapper.store import OperationsStoreError
    except (ImportError, ModuleNotFoundError):
        return ()
    return OperationsStoreError


def _raise_operation_error(error: BaseException) -> None:
    reason = str(getattr(error, "reason_code", "MAPPER_OPERATION_FAILED"))
    detail = str(error)
    if reason in _CONFLICT_CODES:
        raise QueueConflict(f"{reason}: {detail}") from error
    if reason in _NOT_FOUND_CODES:
        raise KeyError(detail)
    raise MapperOperationsError(reason, detail) from error


class MapperOperationsAdapter:
    """Thin, explicit adapter over ``simplicio_mapper.store.OperationsStore``.

    This is an operations primitive, not a DAG planner.  In particular,
    ``claim_next`` preserves Mapper's atomic queue selection and slot capacity
    semantics rather than reimplementing them in Loop.
    """

    schema = "simplicio.loop-mapper-operations-adapter/v1"

    def __init__(
        self,
        database: str | Path,
        *,
        store: Any | None = None,
        auto_create: bool = True,
    ) -> None:
        self.database = Path(database).expanduser().absolute()
        if store is not None:
            self._store = store
            return
        try:
            from simplicio_mapper.store import OperationsStore
        except (ImportError, ModuleNotFoundError) as error:
            raise QueueUnavailable(
                "MapperStore operations API is not installed"
            ) from error
        try:
            self._store = OperationsStore(self.database, auto_create=auto_create)
        except Exception as error:  # constructor must never silently fall back
            raise MapperOperationsError("MAPPER_OPERATION_INIT", str(error)) from error

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return getattr(self._store, method)(*args, **kwargs)
        except _load_store_error_type() as error:
            _raise_operation_error(error)
        except (OSError, TimeoutError) as error:
            raise QueueUnavailable(f"MapperStore unavailable: {error}") from error
        except Exception as error:
            raise MapperOperationsError(
                "MAPPER_OPERATION_FAILED", str(error)
            ) from error

    def initialize(self) -> dict[str, Any]:
        # ``auto_create=False`` is the safe read-only consumer mode.  An
        # explicit initialize call is the one sanctioned transition into the
        # Mapper-owned write mode, so recreate the concrete store with
        # creation enabled instead of silently creating anything in a read.
        if getattr(self._store, "auto_create", None) is False:
            try:
                from simplicio_mapper.store import OperationsStore
            except (ImportError, ModuleNotFoundError) as error:
                raise QueueUnavailable(
                    "MapperStore operations API is not installed"
                ) from error
            self._store = OperationsStore(self.database, auto_create=True)
        return self._call("initialize")

    def capabilities(self) -> dict[str, Any]:
        return self._call("capabilities")

    def register_slot(self, slot_id: str, capacity: int) -> dict[str, Any]:
        return self._call("register_slot", slot_id, capacity)

    def configure_agent_slots(self, capacity: int) -> dict[str, Any]:
        return self._call("configure_agent_slots", capacity)

    def agent_slot_status(self) -> dict[str, Any]:
        return self._call("agent_slot_status")

    def agent_slot_acquire(
        self,
        agent_id: str,
        *,
        worktree: str | None = None,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        return self._call("agent_slot_acquire", agent_id, worktree=worktree, lease_id=lease_id)

    def agent_slot_transition(self, agent_id: str, target: str, *, reason: str = "") -> dict[str, Any]:
        return self._call("agent_slot_transition", agent_id, target, reason=reason)

    def agent_slot_update_blockers(
        self,
        agent_id: str,
        *,
        descendants: int = 0,
        worktree_active: bool = False,
        lease_active: bool = False,
    ) -> dict[str, Any]:
        return self._call(
            "agent_slot_update_blockers",
            agent_id,
            descendants=descendants,
            worktree_active=worktree_active,
            lease_active=lease_active,
        )

    def agent_slot_reclaim(self, agent_id: str | None = None) -> dict[str, Any]:
        return self._call("agent_slot_reclaim", agent_id)

    def enqueue(
        self,
        task_id: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
        priority: int = 0,
    ) -> dict[str, Any]:
        return self._call(
            "enqueue",
            task_id,
            dict(payload),
            idempotency_key=idempotency_key,
            priority=priority,
        )

    def import_task(
        self,
        task_id: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
        state: str = "queued",
        priority: int = 0,
    ) -> dict[str, Any]:
        return self._call(
            "import_task",
            task_id,
            dict(payload),
            idempotency_key=idempotency_key,
            state=state,
            priority=priority,
        )

    @staticmethod
    def _lease(value: Mapping[str, Any]) -> OperationLease:
        return OperationLease(
            task_id=str(value["task_id"]),
            attempt_id=str(value["attempt_id"]),
            fence_token=str(value["fence_token"]),
            lease_id=str(value["lease_id"]),
            worker_id=str(value["worker_id"]),
            payload=dict(value.get("payload") or {}),
            expires_at=(
                float(value["expires_at"])
                if value.get("expires_at") is not None
                else None
            ),
            cancelled=bool(value.get("cancelled", False)),
        )

    def claim_next(
        self,
        worker_id: str,
        *,
        slot_id: str = "default",
        lease_seconds: float = 30.0,
    ) -> OperationLease | None:
        value = self._call(
            "claim", worker_id, slot_id=slot_id, lease_seconds=lease_seconds
        )
        return self._lease(value) if value is not None else None

    def claim_task(
        self,
        task_id: str,
        worker_id: str,
        *,
        slot_id: str = "default",
        lease_seconds: float = 30.0,
    ) -> OperationLease | None:
        value = self._call(
            "claim_task",
            task_id,
            worker_id,
            slot_id=slot_id,
            lease_seconds=lease_seconds,
        )
        return self._lease(value) if value is not None else None

    def list_ready(self, *, limit: int = 20) -> dict[str, Any]:
        return self._call("list_ready", limit=limit)

    def heartbeat(
        self, lease: OperationLease, *, lease_seconds: float = 30.0
    ) -> OperationLease:
        value = self._call(
            "heartbeat",
            lease.attempt_id,
            lease.fence_token,
            lease_seconds=lease_seconds,
        )
        return OperationLease(
            **{
                **lease.__dict__,
                "expires_at": float(value["expires_at"]),
                "cancelled": bool(value.get("cancelled", lease.cancelled)),
            }
        )

    def assert_active(self, lease: OperationLease) -> dict[str, Any]:
        return self._call("assert_active", lease.attempt_id, lease.fence_token)

    def request_cancel(self, task_id: str, *, reason: str = "cancelled") -> dict[str, Any]:
        return self._call("request_cancel", task_id, reason=reason)

    def release(self, lease: OperationLease) -> dict[str, Any]:
        return self._call("release", lease.attempt_id, lease.fence_token)

    def complete(
        self,
        lease: OperationLease,
        *,
        receipt: Mapping[str, Any],
        status: str = "completed",
    ) -> dict[str, Any]:
        return self._call(
            "complete",
            lease.attempt_id,
            lease.fence_token,
            status=status,
            receipt=dict(receipt),
        )

    def cancel(self, task_id: str) -> dict[str, Any]:
        return self._call("cancel", task_id)

    def update_payload(
        self, task_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Update a canonical task payload through MapperStore."""
        return self._call("update_payload", task_id, dict(payload))

    def find_task(self, idempotency_key: str) -> dict[str, Any] | None:
        """Read a canonical task by idempotency key without local storage."""
        return self._call("find_task", idempotency_key)

    def requeue(self, task_id: str) -> dict[str, Any]:
        return self._call("requeue", task_id)

    def reclaim_expired(self) -> dict[str, Any]:
        return self._call("reclaim_expired")

    def checkpoint(
        self,
        lease: OperationLease,
        cursor: int,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._call(
            "checkpoint",
            lease.attempt_id,
            lease.fence_token,
            cursor,
            dict(payload),
        )

    def prepare_effect(
        self,
        lease: OperationLease,
        *,
        effect_id: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._call(
            "prepare_effect",
            lease.attempt_id,
            lease.fence_token,
            effect_id,
            idempotency_key,
            dict(payload),
        )

    def prepare_effect_for_attempt(
        self,
        attempt_id: str,
        fence_token: str,
        *,
        effect_id: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Prepare an effect when the caller owns only attempt identity."""
        return self._call(
            "prepare_effect",
            attempt_id,
            fence_token,
            effect_id,
            idempotency_key,
            dict(payload),
        )

    def commit_effect(
        self,
        lease: OperationLease,
        *,
        effect_id: str,
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._call(
            "commit_effect",
            effect_id,
            lease.attempt_id,
            lease.fence_token,
            dict(receipt),
        )

    def commit_effect_for_attempt(
        self,
        effect_id: str,
        attempt_id: str,
        fence_token: str,
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._call(
            "commit_effect", effect_id, attempt_id, fence_token, dict(receipt)
        )

    def mark_effect_unknown(
        self,
        lease: OperationLease,
        *,
        effect_id: str,
    ) -> dict[str, Any]:
        """Persist an uncertain effect without synthesizing a receipt."""
        return self._call(
            "mark_effect_unknown",
            effect_id,
            lease.attempt_id,
            lease.fence_token,
        )

    def mark_effect_unknown_for_attempt(
        self, effect_id: str, attempt_id: str, fence_token: str
    ) -> dict[str, Any]:
        return self._call(
            "mark_effect_unknown", effect_id, attempt_id, fence_token
        )

    def reconcile_effect(
        self,
        *,
        effect_id: str,
        attempt_id: str,
        outcome: str,
        receipt: Mapping[str, Any],
        fence_token: str | None = None,
    ) -> dict[str, Any]:
        """Resolve an unknown effect using explicit external evidence."""
        return self._call(
            "reconcile_effect",
            effect_id,
            attempt_id,
            fence_token,
            outcome=outcome,
            receipt=dict(receipt),
        )

    def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        expected_seq: int | None = None,
    ) -> dict[str, Any]:
        return self._call(
            "append_event",
            run_id,
            event_type,
            dict(payload),
            expected_seq=expected_seq,
        )

    def replay(self, run_id: str) -> dict[str, Any]:
        return self._call("replay", run_id)

    def status(self, task_id: str | None = None) -> dict[str, Any]:
        return self._call("status", task_id)


__all__ = ["MapperOperationsAdapter", "MapperOperationsError", "OperationLease"]
