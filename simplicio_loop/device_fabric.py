"""Runtime-authoritative CPU/GPU/NPU device fabric for Loop stages.

The Loop owns logical queueing and convergence.  The injected Runtime client
owns physical capacity and opaque leases.  No code in this module imports,
starts, or configures LiteRT/LiteRT-LM or any model provider.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, Mapping, Optional, Protocol, Sequence
from pathlib import Path


SCHEMA = "simplicio.device-fabric/v1"
RECEIPT_SCHEMA = "simplicio.device-execution-receipt/v1"
CAPACITY_SCHEMA = "simplicio.runtime-capacity/v1"
LEASE_SCHEMA = "simplicio.runtime-device-lease/v1"


class FabricError(RuntimeError):
    reason_code = "fabric_error"


class Backpressure(FabricError):
    reason_code = "queue_capacity_exceeded"


class StaleCapacity(FabricError):
    reason_code = "stale_capacity_snapshot"


class CapacityUnavailable(FabricError):
    reason_code = "capacity_unavailable"


class FallbackDenied(FabricError):
    reason_code = "fallback_denied"


class EffectUnknown(FabricError):
    reason_code = "effect_unknown"


class TransientDeviceFailure(FabricError):
    reason_code = "transient_device_failure"


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def write_evidence(directory: str | Path, receipt: Mapping[str, Any]) -> Path:
    """Persist an immutable, hash-checked receipt in the evidence directory."""
    body = dict(receipt)
    declared = body.pop("receipt_hash", None)
    if declared != _hash(body):
        raise ValueError("receipt hash mismatch")
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    request_id = str(receipt.get("request_id") or "device-fabric").replace("/", "_")
    target = root / f"{request_id}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(dict(receipt), sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, target)
    return target


def human_status(status: Mapping[str, Any]) -> str:
    return (
        "device-fabric queued={queued}/{queue_capacity} "
        "in_flight={in_flight}/{max_in_flight} "
        "fallbacks={fallbacks} cancelled={cancelled}"
    ).format(
        **status,
        fallbacks=(status.get("metrics") or {}).get("fallbacks", 0),
        cancelled=(status.get("metrics") or {}).get("cancelled", 0),
    )


def detect_litert(
    distributions: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Inspect package metadata only; never import or start the engine."""
    if distributions is None:
        observed: Dict[str, str] = {}
        for distribution in importlib.metadata.distributions():
            name = str(distribution.metadata.get("Name") or "").lower()
            if name in {"ai-edge-litert", "litert", "litert-lm"}:
                observed[name] = distribution.version
    else:
        observed = {str(key).lower(): str(value) for key, value in distributions.items()}
    engine = next(
        (name for name in ("ai-edge-litert", "litert") if name in observed), None
    )
    return {
        "schema": "simplicio.litert-capability-detection/v1",
        "available": engine is not None,
        "engine_distribution": engine,
        "engine_version": observed.get(engine) if engine else None,
        "lm_distribution": "litert-lm" if "litert-lm" in observed else None,
        "lm_version": observed.get("litert-lm"),
        "reason_code": None if engine else "litert_not_installed",
        "detection": "distribution_metadata_only",
        "engine_imported": False,
        "model_provider_started": False,
    }


@dataclass(frozen=True)
class DeviceRequirement:
    capability: str
    preferred_devices: tuple[str, ...] = ("NPU", "GPU", "CPU")
    allowed_fallback_devices: tuple[str, ...] = ()
    latency_class: str = "interactive"
    memory_class: str = "small"
    memory_bytes: int = 0
    deadline_seconds: float = 30.0
    priority: int = 0

    def __post_init__(self) -> None:
        if self.capability not in {"completion", "embedding", "vision"}:
            raise ValueError("unsupported abstract capability")
        if not self.preferred_devices:
            raise ValueError("at least one preferred device is required")
        if self.memory_bytes < 0 or self.deadline_seconds <= 0:
            raise ValueError("memory/deadline must be bounded")
        if not 0 <= self.priority <= 9:
            raise ValueError("priority must be policy bounded from 0 to 9")


@dataclass(frozen=True)
class DeviceRequest:
    request_id: str
    session_id: str
    owner_id: str
    idempotency_key: str
    requirement: DeviceRequirement

    def __post_init__(self) -> None:
        if not all((
            self.request_id, self.session_id, self.owner_id, self.idempotency_key
        )):
            raise ValueError("request identity fields are required")


Operation = Callable[[asyncio.Event], Awaitable[Any]]


class RuntimeDeviceClient(Protocol):
    async def capacity_snapshot(self) -> Mapping[str, Any]: ...
    async def acquire(
        self, request: DeviceRequest, device: str, snapshot_revision: int
    ) -> Mapping[str, Any]: ...
    async def execute(
        self, lease: Mapping[str, Any], operation: Operation, cancel: asyncio.Event
    ) -> Any: ...
    async def cancel(self, lease: Mapping[str, Any], reason: str) -> None: ...
    async def release(self, lease: Mapping[str, Any], outcome: str) -> None: ...
    async def reconcile(self, idempotency_key: str) -> Mapping[str, Any]: ...
    async def wait_capacity(self) -> None: ...


@dataclass
class _Pending:
    request: DeviceRequest
    operation: Operation
    future: asyncio.Future
    queued_at_ns: int
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    runtime_lease: Optional[Mapping[str, Any]] = None
    task: Optional[asyncio.Task] = None


class DeviceFabric:
    """Bounded fair queue that delegates all physical decisions to Runtime."""

    def __init__(
        self,
        runtime: RuntimeDeviceClient,
        *,
        queue_capacity: int = 64,
        max_in_flight: int = 6,
        capacity_ttl_seconds: float = 5.0,
        max_transient_retries: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if queue_capacity < 1 or max_in_flight < 1:
            raise ValueError("queue and in-flight capacity must be positive")
        self.runtime = runtime
        self.queue_capacity = queue_capacity
        self.max_in_flight = max_in_flight
        self.capacity_ttl_seconds = capacity_ttl_seconds
        self.max_transient_retries = max_transient_retries
        self.clock = clock
        self._sessions: Dict[str, Deque[_Pending]] = {}
        self._round_robin: Deque[str] = deque()
        self._requests: Dict[str, _Pending] = {}
        self._active: Dict[str, _Pending] = {}
        self._wake = asyncio.Event()
        self._closed = False
        self._dispatcher: Optional[asyncio.Task] = None
        self._pressure_limit = max_in_flight
        self.metrics = {
            "submitted": 0, "completed": 0, "cancelled": 0,
            "blocked": 0, "fallbacks": 0, "retries": 0,
            "max_queued": 0, "max_in_flight": 0,
            "pressure_events": 0,
        }

    def start(self) -> None:
        if self._dispatcher is None:
            self._dispatcher = asyncio.create_task(self._dispatch())

    def submit(self, request: DeviceRequest, operation: Operation) -> asyncio.Future:
        if self._closed:
            raise RuntimeError("device fabric is closed")
        if request.request_id in self._requests:
            return self._requests[request.request_id].future
        queued = sum(len(items) for items in self._sessions.values())
        if queued >= self.queue_capacity:
            self.metrics["blocked"] += 1
            raise Backpressure("logical device queue is full")
        loop = asyncio.get_running_loop()
        pending = _Pending(request, operation, loop.create_future(), time.monotonic_ns())
        lane = self._sessions.setdefault(request.session_id, deque())
        lane.append(pending)
        if request.session_id not in self._round_robin:
            self._round_robin.append(request.session_id)
        self._requests[request.request_id] = pending
        self.metrics["submitted"] += 1
        self.metrics["max_queued"] = max(self.metrics["max_queued"], queued + 1)
        self.start()
        self._wake.set()
        return pending.future

    async def cancel(self, request_id: str, reason: str = "cancelled") -> bool:
        pending = self._requests.get(request_id)
        if pending is None or pending.future.done():
            return False
        pending.cancel.set()
        if request_id in self._active:
            if pending.runtime_lease is not None:
                await self.runtime.cancel(pending.runtime_lease, reason)
            if pending.task is not None:
                pending.task.cancel()
        else:
            for session, lane in list(self._sessions.items()):
                try:
                    lane.remove(pending)
                except ValueError:
                    continue
                if not lane:
                    self._sessions.pop(session, None)
                    try:
                        self._round_robin.remove(session)
                    except ValueError:
                        pass
                break
            self._finish_cancelled(pending, reason, queue_only=True)
        self._wake.set()
        return True

    async def close(self) -> None:
        self._closed = True
        for request_id in list(self._requests):
            await self.cancel(request_id, "fabric_closed")
        self._wake.set()
        if self._dispatcher is not None:
            await self._dispatcher

    def status(self) -> Dict[str, Any]:
        return {
            "schema": SCHEMA,
            "queue_capacity": self.queue_capacity,
            "queued": sum(len(items) for items in self._sessions.values()),
            "in_flight": len(self._active),
            "max_in_flight": self.max_in_flight,
            "sessions": {
                key: [item.request.request_id for item in value]
                for key, value in sorted(self._sessions.items())
            },
            "metrics": dict(self.metrics),
            "runtime_authority": True,
            "loop_spawns_engine": False,
            "model_provider_started": False,
        }

    def _pop_fair(self) -> Optional[_Pending]:
        while self._round_robin:
            session = self._round_robin.popleft()
            lane = self._sessions.get(session)
            if not lane:
                self._sessions.pop(session, None)
                continue
            pending = lane.popleft()
            if lane:
                self._round_robin.append(session)
            else:
                self._sessions.pop(session, None)
            return pending
        return None

    async def _dispatch(self) -> None:
        while True:
            launched = False
            while len(self._active) < min(self.max_in_flight, self._pressure_limit):
                pending = self._pop_fair()
                if pending is None:
                    break
                if pending.cancel.is_set():
                    self._finish_cancelled(pending, "cancelled_before_admission", True)
                    continue
                self._active[pending.request.request_id] = pending
                pending.task = asyncio.create_task(self._run(pending))
                self.metrics["max_in_flight"] = max(
                    self.metrics["max_in_flight"], len(self._active)
                )
                launched = True
            if self._closed and not self._active and not self._round_robin:
                return
            if not launched:
                self._wake.clear()
                await self._wake.wait()

    async def _snapshot(self) -> Mapping[str, Any]:
        snapshot = dict(await self.runtime.capacity_snapshot())
        if snapshot.get("schema") != CAPACITY_SCHEMA:
            raise StaleCapacity("capacity schema mismatch")
        observed = float(snapshot.get("observed_at", -1))
        if self.clock() - observed > self.capacity_ttl_seconds:
            raise StaleCapacity("capacity snapshot TTL expired")
        if not isinstance(snapshot.get("revision"), int):
            raise StaleCapacity("capacity revision missing")
        pressure = any(
            bool(row.get("pressure"))
            for row in (snapshot.get("devices") or {}).values()
            if isinstance(row, Mapping)
        )
        new_limit = max(1, self.max_in_flight // 2) if pressure else self.max_in_flight
        if pressure and new_limit != self._pressure_limit:
            self.metrics["pressure_events"] += 1
        self._pressure_limit = new_limit
        return snapshot

    @staticmethod
    def _candidate_devices(
        request: DeviceRequest, snapshot: Mapping[str, Any]
    ) -> list[str]:
        devices = snapshot.get("devices") or {}
        requirement = request.requirement
        candidates = []
        for index, device in enumerate(requirement.preferred_devices):
            row = devices.get(device) or {}
            if requirement.capability not in row.get("capabilities", ()):
                continue
            if index > 0 and device not in requirement.allowed_fallback_devices:
                continue
            candidates.append(device)
        return candidates

    async def _run(self, pending: _Pending) -> None:
        request = pending.request
        started_ns = time.monotonic_ns()
        queue_ns = started_ns - pending.queued_at_ns
        lease = None
        effective_device = None
        fallback = False
        attempt = 0
        try:
            admission_deadline = self.clock() + request.requirement.deadline_seconds
            while lease is None:
                if pending.cancel.is_set():
                    raise asyncio.CancelledError
                snapshot = await self._snapshot()
                candidates = self._candidate_devices(request, snapshot)
                if not candidates:
                    primary = request.requirement.preferred_devices[0]
                    primary_row = (snapshot.get("devices") or {}).get(primary) or {}
                    if (
                        request.requirement.capability
                        not in primary_row.get("capabilities", ())
                        and request.requirement.allowed_fallback_devices
                    ):
                        raise CapacityUnavailable("no allowed device has the capability")
                    raise FallbackDenied(
                        "primary unavailable and no policy-approved fallback"
                    )
                for device in candidates:
                    try:
                        lease = await self.runtime.acquire(
                            request, device, int(snapshot["revision"])
                        )
                        effective_device = device
                        break
                    except (CapacityUnavailable, StaleCapacity):
                        continue
                if lease is None:
                    if self.clock() >= admission_deadline:
                        raise CapacityUnavailable("device wait deadline exceeded")
                    await self.runtime.wait_capacity()
            pending.runtime_lease = lease
            fallback = effective_device != request.requirement.preferred_devices[0]
            if fallback:
                self.metrics["fallbacks"] += 1
            execution_started = time.monotonic_ns()
            queue_ns = execution_started - pending.queued_at_ns
            while True:
                attempt += 1
                try:
                    result = await asyncio.wait_for(
                        self.runtime.execute(lease, pending.operation, pending.cancel),
                        timeout=request.requirement.deadline_seconds,
                    )
                    break
                except TransientDeviceFailure:
                    if attempt > self.max_transient_retries:
                        raise
                    self.metrics["retries"] += 1
                    continue
                except EffectUnknown:
                    reconciled = dict(await self.runtime.reconcile(request.idempotency_key))
                    if reconciled.get("terminal") is True:
                        result = reconciled.get("result")
                        break
                    raise
            execution_ns = time.monotonic_ns() - execution_started
            receipt = self._receipt(
                pending, "succeeded", queue_ns, execution_ns,
                snapshot, lease, effective_device, fallback, attempt,
                result=result,
            )
            if not pending.future.done():
                pending.future.set_result(receipt)
            self.metrics["completed"] += 1
        except asyncio.CancelledError:
            if lease is not None:
                await self.runtime.release(lease, "cancelled")
                lease = None
            self._finish_cancelled(pending, "cancelled_running", queue_only=False)
        except Exception as exc:
            receipt = {
                "schema": RECEIPT_SCHEMA,
                "request_id": request.request_id,
                "status": "blocked",
                "reason_code": getattr(exc, "reason_code", type(exc).__name__),
                "requested": self._requested(request),
                "queue_time_ns": queue_ns,
                "execution_time_ns": None,
                "execution_time_reason": "execution_not_completed",
                "runtime_authority": True,
                "loop_spawns_engine": False,
                "model_provider_started": False,
            }
            receipt["receipt_hash"] = _hash(receipt)
            if not pending.future.done():
                pending.future.set_result(receipt)
            self.metrics["blocked"] += 1
        finally:
            if lease is not None:
                await self.runtime.release(lease, "terminal")
            self._active.pop(request.request_id, None)
            self._requests.pop(request.request_id, None)
            self._wake.set()

    @staticmethod
    def _requested(request: DeviceRequest) -> Dict[str, Any]:
        requirement = request.requirement
        return {
            "capability": requirement.capability,
            "preferred_devices": list(requirement.preferred_devices),
            "allowed_fallback_devices": list(requirement.allowed_fallback_devices),
            "latency_class": requirement.latency_class,
            "memory_class": requirement.memory_class,
            "memory_bytes": requirement.memory_bytes,
        }

    def _receipt(
        self, pending: _Pending, status: str, queue_ns: int, execution_ns: int,
        snapshot: Mapping[str, Any], lease: Mapping[str, Any],
        effective_device: str, fallback: bool, attempt: int, *, result: Any,
    ) -> Dict[str, Any]:
        request = pending.request
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "request_id": request.request_id,
            "session_id": request.session_id,
            "owner_id": request.owner_id,
            "idempotency_key": request.idempotency_key,
            "status": status,
            "reason_code": None,
            "requested": self._requested(request),
            "effective": {
                "device": effective_device,
                "backend": lease.get("backend"),
                "capabilities": list(lease.get("capabilities") or ()),
            },
            "fallback": {
                "used": fallback,
                "policy_allowed": (
                    effective_device in request.requirement.allowed_fallback_devices
                    if fallback else True
                ),
            },
            "capacity_revision": snapshot["revision"],
            "lease": {
                "lease_id": lease.get("lease_id"),
                "fence": lease.get("fence"),
                "deadline": lease.get("deadline"),
            },
            "queue_time_ns": queue_ns,
            "execution_time_ns": execution_ns,
            "attempts": attempt,
            "result_hash": _hash(result),
            "runtime_authority": True,
            "loop_spawns_engine": False,
            "model_provider_started": False,
        }
        receipt["receipt_hash"] = _hash(receipt)
        return receipt

    def _finish_cancelled(
        self, pending: _Pending, reason: str, queue_only: bool
    ) -> None:
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "request_id": pending.request.request_id,
            "status": "cancelled",
            "reason_code": reason,
            "requested": self._requested(pending.request),
            "queue_time_ns": time.monotonic_ns() - pending.queued_at_ns,
            "execution_time_ns": None,
            "execution_time_reason": (
                "never_started" if queue_only else "cancelled_before_completion"
            ),
            "runtime_authority": True,
            "loop_spawns_engine": False,
            "model_provider_started": False,
        }
        receipt["receipt_hash"] = _hash(receipt)
        if not pending.future.done():
            pending.future.set_result(receipt)
        self.metrics["cancelled"] += 1
        self._requests.pop(pending.request.request_id, None)


class DeviceStageAdapter:
    """Bind a real Loop stage identity to one device-fabric request."""

    def __init__(self, fabric: DeviceFabric) -> None:
        self.fabric = fabric

    async def run(
        self, *, stage_id: str, stage_instance_id: str,
        request: DeviceRequest, operation: Operation,
    ) -> Dict[str, Any]:
        if not stage_id or not stage_instance_id:
            raise ValueError("stage identity is required")
        device_receipt = await self.fabric.submit(request, operation)
        status = device_receipt["status"]
        receipt = {
            "schema": "simplicio.device-stage-receipt/v1",
            "stage_id": stage_id,
            "stage_instance_id": stage_instance_id,
            "request_id": request.request_id,
            "status": "pass" if status == "succeeded" else status,
            "reason_code": device_receipt.get("reason_code"),
            "device_receipt_hash": device_receipt["receipt_hash"],
            "device_receipt": device_receipt,
            "runtime_authority": True,
            "loop_spawns_engine": False,
            "model_provider_started": False,
        }
        receipt["receipt_hash"] = _hash(receipt)
        return receipt


class FakeRuntimeDeviceAuthority:
    """Deterministic shared Runtime authority used by conformance/E2E tests."""

    def __init__(
        self,
        devices: Mapping[str, Mapping[str, Any]],
        *,
        clock: Callable[[], float] = time.monotonic,
        litert_available: bool = True,
    ) -> None:
        self.clock = clock
        self.devices = {name: dict(value) for name, value in devices.items()}
        self.litert_available = litert_available
        self.revision = 1
        self._lock = asyncio.Lock()
        self._capacity = asyncio.Condition()
        self._leases: Dict[str, Dict[str, Any]] = {}
        self._slots: Dict[str, set[int]] = {name: set() for name in devices}
        self._fence = 0
        self._terminal: Dict[str, Any] = {}
        self.max_used = {name: 0 for name in devices}
        self.cancelled: list[str] = []
        self.execution_calls: Dict[str, int] = {}

    async def capacity_snapshot(self) -> Mapping[str, Any]:
        async with self._lock:
            rows = {}
            for name, config in self.devices.items():
                capacity = int(config.get("slots", 0))
                used = len(self._slots[name])
                memory = int(config.get("memory_bytes", 0))
                memory_used = sum(
                    int(lease.get("memory_bytes", 0))
                    for lease in self._leases.values()
                    if lease["device"] == name
                )
                rows[name] = {
                    "slots_total": capacity,
                    "slots_available": capacity - used,
                    "memory_total_bytes": memory,
                    "memory_available_bytes": memory - memory_used,
                    "capabilities": list(config.get("capabilities") or ()),
                    "backend": config.get("backend", "fake-litert"),
                    "pressure": memory > 0 and memory_used / memory >= 0.8,
                }
            return {
                "schema": CAPACITY_SCHEMA,
                "revision": self.revision,
                "observed_at": self.clock(),
                "litert_available": self.litert_available,
                "devices": rows,
                "authority": "runtime",
            }

    async def acquire(
        self, request: DeviceRequest, device: str, snapshot_revision: int
    ) -> Mapping[str, Any]:
        async with self._lock:
            if snapshot_revision != self.revision:
                raise StaleCapacity("Runtime capacity revision changed")
            if not self.litert_available:
                raise CapacityUnavailable("LiteRT unavailable in Runtime")
            if request.idempotency_key in self._terminal or any(
                lease["idempotency_key"] == request.idempotency_key
                for lease in self._leases.values()
            ):
                raise EffectUnknown("causal identity already active or terminal")
            config = self.devices.get(device)
            if config is None:
                raise CapacityUnavailable("device unavailable")
            if request.requirement.capability not in config.get("capabilities", ()):
                raise CapacityUnavailable("capability unavailable")
            slots = int(config.get("slots", 0))
            available = next(
                (slot for slot in range(slots) if slot not in self._slots[device]), None
            )
            memory_used = sum(
                int(lease.get("memory_bytes", 0))
                for lease in self._leases.values() if lease["device"] == device
            )
            if (
                available is None
                or memory_used + request.requirement.memory_bytes
                > int(config.get("memory_bytes", 0))
            ):
                raise CapacityUnavailable("physical device capacity exhausted")
            self._fence += 1
            lease_id = "runtime-lease-" + uuid.uuid4().hex
            lease = {
                "schema": LEASE_SCHEMA,
                "lease_id": lease_id,
                "device": device,
                "slot": available,
                "owner_id": request.owner_id,
                "request_id": request.request_id,
                "idempotency_key": request.idempotency_key,
                "fence": self._fence,
                "deadline": self.clock() + request.requirement.deadline_seconds,
                "memory_bytes": request.requirement.memory_bytes,
                "backend": config.get("backend", "fake-litert"),
                "capabilities": list(config.get("capabilities") or ()),
            }
            self._slots[device].add(available)
            self._leases[lease_id] = lease
            self.max_used[device] = max(self.max_used[device], len(self._slots[device]))
            return dict(lease)

    async def execute(
        self, lease: Mapping[str, Any], operation: Operation, cancel: asyncio.Event
    ) -> Any:
        lease_id = str(lease["lease_id"])
        async with self._lock:
            current = self._leases.get(lease_id)
            if current is None or current["fence"] != lease["fence"]:
                raise StaleCapacity("stale Runtime lease")
        key = str(lease["idempotency_key"])
        self.execution_calls[key] = self.execution_calls.get(key, 0) + 1
        result = await operation(cancel)
        self._terminal[key] = result
        return result

    async def cancel(self, lease: Mapping[str, Any], reason: str) -> None:
        self.cancelled.append(str(lease["lease_id"]))

    async def release(self, lease: Mapping[str, Any], outcome: str) -> None:
        async with self._lock:
            removed = self._leases.pop(str(lease["lease_id"]), None)
            if removed is not None:
                self._slots[removed["device"]].discard(int(removed["slot"]))
                self.revision += 1
        async with self._capacity:
            self._capacity.notify_all()

    async def reconcile(self, idempotency_key: str) -> Mapping[str, Any]:
        if idempotency_key in self._terminal:
            return {
                "terminal": True, "result": self._terminal[idempotency_key],
                "source": "runtime_receipt",
            }
        return {"terminal": False, "reason_code": "effect_unknown"}

    async def wait_capacity(self) -> None:
        async with self._capacity:
            try:
                await asyncio.wait_for(self._capacity.wait(), timeout=0.05)
            except asyncio.TimeoutError:
                return
