"""Standalone resource authority for processes, workers and bounded capacity.

``ResourceFabric`` is the Loop-side boundary described by
``simplicio.resource-fabric/v1``.  It does not start a process until an
authoritative fenced lease exists, supports an explicit Runtime takeover, and
uses ``PythonProcessAdapter`` for kill-tree/cancellation semantics.  Runtime
transport is intentionally injected at the takeover boundary; this module never
guesses that a Runtime is available.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .process_supervisor import (
    ProcessLease,
    ProcessResult,
    ProcessSpec,
    PythonProcessAdapter,
)
from .slot_lease import LeaseConflict, LeaseStore, StaleFence

RESOURCE_FABRIC_SCHEMA = "simplicio.resource-fabric/v1"
AUTHORITY_SCHEMA = "simplicio.resource-authority/v1"
TAKEOVER_SCHEMA = "simplicio.resource-takeover/v1"


class ResourceFabricError(RuntimeError):
    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        super().__init__(f"{reason_code}: {detail}" if detail else reason_code)


class AuthorityConflict(ResourceFabricError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("AUTHORITY_CONFLICT", detail)


class CapacityExceeded(ResourceFabricError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("CAPACITY_EXCEEDED", detail)


class FabricDraining(ResourceFabricError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("FABRIC_DRAINING", detail)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class HostCapacity:
    """Conservative host envelope; explicit values beat platform heuristics."""

    cpu_units: int = 1
    memory_bytes: int = 0
    io_units: int = 1
    process_slots: int = 1
    gpu_units: int = 0
    npu_units: int = 0
    source: str = "explicit"

    def __post_init__(self) -> None:
        for name in ("cpu_units", "memory_bytes", "io_units", "process_slots", "gpu_units", "npu_units"):
            value = getattr(self, name)
            if isinstance(value, bool) or int(value) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.cpu_units < 1 or self.io_units < 1 or self.process_slots < 1:
            raise ValueError("cpu_units, io_units and process_slots must be positive")

    @classmethod
    def from_environment(cls) -> HostCapacity:
        def integer(name: str, default: int) -> int:
            raw = os.environ.get(name, "").strip()
            if not raw:
                return default
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError(f"{name} must be an integer") from exc
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
            return value

        # os.cpu_count is only a safe upper hint; process admission remains
        # bounded by the explicit fabric slots and memory/I/O budgets.
        cpu_hint = max(1, min(os.cpu_count() or 1, 8))
        return cls(
            cpu_units=integer("SIMPLICIO_RESOURCE_CPU_UNITS", cpu_hint),
            memory_bytes=integer("SIMPLICIO_RESOURCE_MEMORY_BYTES", 0),
            io_units=integer("SIMPLICIO_RESOURCE_IO_UNITS", 1),
            process_slots=integer("SIMPLICIO_RESOURCE_PROCESS_SLOTS", cpu_hint),
            gpu_units=integer("SIMPLICIO_RESOURCE_GPU_UNITS", 0),
            npu_units=integer("SIMPLICIO_RESOURCE_NPU_UNITS", 0),
            source="environment",
        )

    def limit(self, resource_class: str) -> int:
        values = {
            "cpu": self.cpu_units,
            "memory": self.memory_bytes,
            "io": self.io_units,
            "process": self.process_slots,
            "gpu": self.gpu_units,
            "npu": self.npu_units,
        }
        if resource_class not in values:
            raise ValueError(f"unknown resource class: {resource_class}")
        return values[resource_class]

    def as_dict(self) -> dict[str, Any]:
        return {
            "cpu_units": self.cpu_units,
            "memory_bytes": self.memory_bytes,
            "io_units": self.io_units,
            "process_slots": self.process_slots,
            "gpu_units": self.gpu_units,
            "npu_units": self.npu_units,
            "source": self.source,
        }


@dataclass(frozen=True)
class ResourceRequest:
    request_id: str
    resource_class: str
    units: int = 1
    owner_id: str = "loop"
    ttl_seconds: float = 30.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.request_id).strip() or not str(self.owner_id).strip():
            raise ValueError("request_id and owner_id are required")
        if self.resource_class not in {"cpu", "memory", "io", "process", "gpu", "npu"}:
            raise ValueError("unsupported resource class")
        if isinstance(self.units, bool) or self.units < 1:
            raise ValueError("units must be positive")
        if self.ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def resource_key(self) -> str:
        return f"resource:{self.resource_class}:{self.request_id}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": RESOURCE_FABRIC_SCHEMA,
            "request_id": self.request_id,
            "resource_key": self.resource_key,
            "resource_class": self.resource_class,
            "units": self.units,
            "owner_id": self.owner_id,
            "ttl_seconds": self.ttl_seconds,
            "metadata": dict(self.metadata),
        }


class ResourceFabric:
    """One physical authority per host with durable leases and bounded admission."""

    def __init__(
        self,
        root: str | Path,
        *,
        host_id: str = "local",
        owner_id: str = "loop",
        capacity: HostCapacity | None = None,
        lease_store: LeaseStore | None = None,
        process_adapter: PythonProcessAdapter | None = None,
        clock=time.time,
    ) -> None:
        self.root = Path(root).expanduser().absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self.host_id = str(host_id).strip() or "local"
        self.owner_id = str(owner_id).strip() or "loop"
        self.capacity = capacity or HostCapacity.from_environment()
        self.clock = clock
        self.leases = lease_store or LeaseStore(self.root / "resource-fabric.sqlite", clock=clock)
        self.process_adapter = process_adapter or PythonProcessAdapter()
        self._authority_key = f"authority:{self.host_id}"
        self._authority: dict[str, Any] | None = None
        self._draining = False
        self._lock = threading.RLock()
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._processes: dict[str, dict[str, Any]] = {}

    def start(self, *, ttl_seconds: float = 30.0) -> dict[str, Any]:
        with self._lock:
            try:
                acquired = self.leases.acquire(self._authority_key, self.owner_id, ttl_seconds=ttl_seconds)
            except LeaseConflict as exc:
                raise AuthorityConflict(str(exc)) from exc
            self._authority = acquired["lease"]
            self._draining = False
            self._persist_state()
            return self.authority_receipt("AUTHORITY_READY")

    def _require_authority(self) -> dict[str, Any]:
        if self._authority is None:
            raise AuthorityConflict("fabric has not acquired host authority")
        status = self.leases.status(self._authority_key)
        if not status or status.get("state") != "active" or status.get("owner_id") != self.owner_id:
            raise AuthorityConflict("host authority is stale or owned by another supervisor")
        self._authority = dict(status)
        return self._authority

    def heartbeat_authority(self, *, ttl_seconds: float = 30.0) -> dict[str, Any]:
        authority = self._require_authority()
        renewed = self.leases.heartbeat(
            self._authority_key, self.owner_id, int(authority["fence"]), ttl_seconds=ttl_seconds
        )
        self._authority = renewed["lease"]
        return self.authority_receipt("AUTHORITY_HEARTBEAT", receipt=renewed["receipt"])

    def authority_receipt(self, event: str, *, receipt: Mapping[str, Any] | None = None) -> dict[str, Any]:
        authority = self._authority or self.leases.status(self._authority_key)
        body = {
            "schema": AUTHORITY_SCHEMA,
            "event": event,
            "host_id": self.host_id,
            "owner_id": self.owner_id,
            "authority": authority,
            "receipt": dict(receipt or {}),
        }
        body["receipt_hash"] = _digest(body)
        return body

    def prepare_takeover(self, new_owner_id: str) -> dict[str, Any]:
        authority = self._require_authority()
        payload = {
            "schema": TAKEOVER_SCHEMA,
            "host_id": self.host_id,
            "current_owner_id": self.owner_id,
            "current_fence": int(authority["fence"]),
            "new_owner_id": str(new_owner_id).strip(),
        }
        if not payload["new_owner_id"]:
            raise ValueError("new_owner_id is required")
        return {**payload, "prepared_token": _digest(payload), "status": "PREPARED"}

    def takeover(self, handshake: Mapping[str, Any], *, ttl_seconds: float = 30.0) -> dict[str, Any]:
        payload = {key: handshake.get(key) for key in (
            "schema", "host_id", "current_owner_id", "current_fence", "new_owner_id"
        )}
        if handshake.get("schema") != TAKEOVER_SCHEMA or handshake.get("prepared_token") != _digest(payload):
            raise ResourceFabricError("TAKEOVER_INVALID", "prepared handshake does not match")
        if payload["host_id"] != self.host_id or payload["current_owner_id"] != self.owner_id:
            raise ResourceFabricError("TAKEOVER_STALE", "takeover owner or host changed")
        authority = self._require_authority()
        if int(payload["current_fence"]) != int(authority["fence"]):
            raise ResourceFabricError("TAKEOVER_STALE", "authority fence changed")
        invalidated = self.leases.invalidate_owner(self.owner_id, reason="runtime_takeover")
        try:
            acquired = self.leases.acquire(
                self._authority_key, str(payload["new_owner_id"]), ttl_seconds=ttl_seconds
            )
        except LeaseConflict as exc:
            raise AuthorityConflict("new authority could not be acquired") from exc
        self.owner_id = str(payload["new_owner_id"])
        self._authority = acquired["lease"]
        self._draining = False
        self._persist_state()
        return {
            **self.authority_receipt("AUTHORITY_TAKEOVER", receipt=acquired["receipt"]),
            "invalidated_claims": len(invalidated),
            "previous_owner_id": payload["current_owner_id"],
        }

    @contextmanager
    def _admission_guard(self, resource_class: str) -> Iterator[None]:
        guard_key = f"admission:{self.host_id}:{resource_class}"
        guard_owner = f"{self.owner_id}:guard:{uuid.uuid4().hex}"
        try:
            guard = self.leases.acquire(guard_key, guard_owner, ttl_seconds=5.0)
        except LeaseConflict as exc:
            raise ResourceFabricError("ADMISSION_BUSY", resource_class) from exc
        try:
            yield
        finally:
            try:
                self.leases.release(guard_key, guard_owner, int(guard["lease"]["fence"]))
            except StaleFence:
                pass

    def _active_units(self, resource_class: str) -> int:
        total = 0
        for row in self.leases.list_leases(prefix=f"resource:{resource_class}:"):
            if row.get("state") != "active":
                continue
            metadata = self.leases.read(row["resource_key"], "request") or {}
            total += int(metadata.get("units", 1))
        return total

    def claim(self, request: ResourceRequest) -> dict[str, Any]:
        if request.owner_id != self.owner_id:
            raise AuthorityConflict("request owner is not the current authority")
        if self._draining:
            raise FabricDraining()
        self._require_authority()
        limit = self.capacity.limit(request.resource_class)
        if limit < request.units:
            raise CapacityExceeded(f"{request.resource_class} request exceeds host limit")
        with self._admission_guard(request.resource_class):
            current = self._active_units(request.resource_class)
            if current + request.units > limit:
                raise CapacityExceeded(f"{request.resource_class} capacity {current + request.units}>{limit}")
            try:
                acquired = self.leases.acquire(
                    request.resource_key, request.owner_id, ttl_seconds=request.ttl_seconds
                )
                lease = acquired["lease"]
                self.leases.put(
                    request.resource_key, request.owner_id, int(lease["fence"]),
                    "request", request.as_dict(),
                )
            except (KeyError, OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
                try:
                    if "lease" in locals():
                        self.leases.release(request.resource_key, request.owner_id, int(lease["fence"]))
                except (KeyError, OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
                    pass
                raise
        return {"schema": RESOURCE_FABRIC_SCHEMA, "status": "CLAIMED", "request": request.as_dict(), "lease": lease, "receipt": acquired["receipt"]}

    def heartbeat(self, claim: Mapping[str, Any], *, ttl_seconds: float = 30.0) -> dict[str, Any]:
        request = claim.get("request") or {}
        lease = claim.get("lease") or {}
        if not request or not lease:
            raise ResourceFabricError("CLAIM_MALFORMED")
        renewed = self.leases.heartbeat(
            str(lease["resource_key"]), str(lease["owner_id"]), int(lease["fence"]), ttl_seconds=ttl_seconds
        )
        return {**claim, "lease": renewed["lease"], "receipt": renewed["receipt"]}

    def release(self, claim: Mapping[str, Any]) -> dict[str, Any]:
        lease = claim.get("lease") or {}
        released = self.leases.release(
            str(lease["resource_key"]), str(lease["owner_id"]), int(lease["fence"])
        )
        return {"schema": RESOURCE_FABRIC_SCHEMA, "status": "RELEASED", "lease": released["lease"], "receipt": released["receipt"]}

    async def spawn(self, request: ResourceRequest, spec: ProcessSpec) -> dict[str, Any]:
        claim = self.claim(request)
        lease = claim["lease"]
        process_lease = ProcessLease(
            lease_id=f"{lease['resource_key']}:{lease['fence']}",
            spec_hash=spec.spec_hash,
            ttl_seconds=request.ttl_seconds,
        )
        task = asyncio.current_task()
        if task is not None:
            self._tasks[request.request_id] = task

        def on_spawned(process: asyncio.subprocess.Process) -> None:
            self._processes[request.request_id] = {
                "pid": process.pid,
                "resource_key": lease["resource_key"],
                "fence": lease["fence"],
                "argv0": spec.argv[0],
            }

        try:
            result = await self.process_adapter.run(spec, lease=process_lease, on_spawned=on_spawned)
        finally:
            self._tasks.pop(request.request_id, None)
            self._processes.pop(request.request_id, None)
        try:
            released = self.release(claim)
            status = "COMPLETED"
        except StaleFence as exc:
            released = {"error": str(exc)}
            status = "BLOCKED"
        return {
            "schema": RESOURCE_FABRIC_SCHEMA,
            "status": status,
            "request_id": request.request_id,
            "resource_class": request.resource_class,
            "process": result.to_dict() if isinstance(result, ProcessResult) else result,
            "release": released,
        }

    async def drain(self, *, reason: str = "stop", timeout_seconds: float = 10.0) -> dict[str, Any]:
        self._draining = True
        self.leases.invalidate_owner(self.owner_id, reason=reason)
        current = asyncio.current_task()
        tasks = [task for task in self._tasks.values() if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=timeout_seconds)
        self._persist_state()
        return self.status(event="DRAINED")

    def reconcile(self) -> dict[str, Any]:
        stale = self.leases.mark_stale()
        observed = self.leases.list_leases(prefix="resource:")
        return {
            "schema": RESOURCE_FABRIC_SCHEMA,
            "status": "RECONCILED",
            "stale_count": len(stale),
            "leases": observed,
            "processes": list(self._processes.values()),
        }

    def status(self, *, event: str = "STATUS") -> dict[str, Any]:
        authority = self.leases.status(self._authority_key)
        usage = {
            resource_class: self._active_units(resource_class)
            for resource_class in ("cpu", "memory", "io", "process", "gpu", "npu")
        }
        return {
            "schema": RESOURCE_FABRIC_SCHEMA,
            "event": event,
            "host_id": self.host_id,
            "owner_id": self.owner_id,
            "platform": platform.system().lower(),
            "draining": self._draining,
            "authority": authority,
            "capacity": self.capacity.as_dict(),
            "usage": usage,
            "leases": self.leases.list_leases(prefix="resource:"),
            "processes": list(self._processes.values()),
            "queue": {"running": len(self._tasks), "bounded": True},
        }

    def _persist_state(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "resource-fabric.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.status(), ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)


__all__ = [
    "AUTHORITY_SCHEMA",
    "RESOURCE_FABRIC_SCHEMA",
    "TAKEOVER_SCHEMA",
    "AuthorityConflict",
    "CapacityExceeded",
    "FabricDraining",
    "HostCapacity",
    "ResourceFabric",
    "ResourceFabricError",
    "ResourceRequest",
]
