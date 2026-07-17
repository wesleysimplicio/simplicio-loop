"""Fail-closed resource admission and protection for the Hub.

The governor is intentionally stdlib-only and standalone-safe. It owns logical
budgets and leases; platform-specific probes may feed failure signals, while
the admission contract remains deterministic when a probe is unavailable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Dict, Mapping, Optional


GOVERNOR_SCHEMA = "simplicio.hub-resource-governor/v1"
RESOURCE_NAMES = (
    "cpu",
    "memory_bytes",
    "disk_bytes",
    "gpu",
    "processes",
    "connections",
    "tokens",
)


class GovernorError(RuntimeError):
    """Base error for resource admission."""


class ResourceThrottled(GovernorError):
    """Admission was refused because a limit, circuit, or drain is active."""

    def __init__(self, receipt: Mapping[str, Any]) -> None:
        self.receipt = dict(receipt)
        super().__init__(str(self.receipt.get("reason", "resource throttled")))


@dataclass(frozen=True)
class ResourceLimits:
    cpu: int = 0
    memory_bytes: int = 0
    disk_bytes: int = 0
    gpu: int = 0
    processes: int = 0
    connections: int = 0
    tokens: int = 0

    def __post_init__(self) -> None:
        if any(value < 0 for value in self.as_dict().values()):
            raise ValueError("resource limits must be non-negative")

    def as_dict(self) -> Dict[str, int]:
        return {name: int(getattr(self, name)) for name in RESOURCE_NAMES}


@dataclass(frozen=True)
class ResourceRequest:
    cpu: int = 0
    memory_bytes: int = 0
    disk_bytes: int = 0
    gpu: int = 0
    processes: int = 0
    connections: int = 0
    tokens: int = 0

    def __post_init__(self) -> None:
        if any(value < 0 for value in self.as_dict().values()):
            raise ValueError("resource requests must be non-negative")

    def as_dict(self) -> Dict[str, int]:
        return {name: int(getattr(self, name)) for name in RESOURCE_NAMES}


@dataclass(frozen=True)
class ResourceLease:
    lease_id: str
    client_id: str
    task_id: str
    request: ResourceRequest
    admitted_at: float
    queue: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schema": "simplicio.hub-resource-lease/v1",
            "lease_id": self.lease_id,
            "client_id": self.client_id,
            "task_id": self.task_id,
            "request": self.request.as_dict(),
            "admitted_at": self.admitted_at,
            "queue": self.queue,
        }


class CircuitBreaker:
    """Trip after repeated pressure signals and recover after a cooldown."""

    def __init__(self, *, threshold: int = 3, cooldown_seconds: float = 30.0) -> None:
        if threshold < 1 or cooldown_seconds < 0:
            raise ValueError("circuit settings must be valid")
        self.threshold = threshold
        self.cooldown_seconds = float(cooldown_seconds)
        self.failures = 0
        self.state = "closed"
        self.reason = ""
        self.tripped_at = 0.0

    def allow(self, now: Optional[float] = None) -> bool:
        current = time.monotonic() if now is None else now
        if self.state == "open" and current - self.tripped_at >= self.cooldown_seconds:
            self.state = "half_open"
        return self.state != "open"

    def record_failure(self, reason: str, now: Optional[float] = None) -> bool:
        current = time.monotonic() if now is None else now
        self.failures += 1
        self.reason = reason
        if self.failures >= self.threshold:
            self.state = "open"
            self.tripped_at = current
        return self.state == "open"

    def recover(self) -> None:
        self.failures = 0
        self.state = "closed"
        self.reason = ""
        self.tripped_at = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "failures": self.failures,
            "threshold": self.threshold,
            "reason": self.reason,
            "tripped_at": self.tripped_at,
            "cooldown_seconds": self.cooldown_seconds,
        }


@dataclass
class _Usage:
    values: Dict[str, int] = field(default_factory=lambda: {name: 0 for name in RESOURCE_NAMES})

    def add(self, request: ResourceRequest) -> None:
        for name, value in request.as_dict().items():
            self.values[name] += value

    def subtract(self, request: ResourceRequest) -> None:
        for name, value in request.as_dict().items():
            self.values[name] = max(0, self.values[name] - value)


class ResourceGovernor:
    """Atomically admits bounded work and emits safe throttle receipts."""

    def __init__(
        self,
        limits: ResourceLimits,
        *,
        client_limits: Optional[Mapping[str, ResourceLimits]] = None,
        circuit_threshold: int = 3,
        cooldown_seconds: float = 30.0,
    ) -> None:
        self.limits = limits
        self.client_limits = dict(client_limits or {})
        self._usage = _Usage()
        self._client_usage: Dict[str, _Usage] = {}
        self._leases: Dict[str, ResourceLease] = {}
        self._receipts: list[Dict[str, Any]] = []
        self._lock = RLock()
        self._draining = False
        self._circuit = CircuitBreaker(
            threshold=circuit_threshold, cooldown_seconds=cooldown_seconds
        )

    @staticmethod
    def _over_budget(
        used: Mapping[str, int], request: ResourceRequest, limits: ResourceLimits
    ) -> Optional[str]:
        for name, value in request.as_dict().items():
            limit = limits.as_dict()[name]
            if limit and int(used.get(name, 0)) + value > limit:
                return name
        return None

    def _receipt(
        self,
        *,
        client_id: str,
        task_id: str,
        reason: str,
        resource: str = "",
        requested: int = 0,
        available: int = 0,
        queue: str = "",
    ) -> Dict[str, Any]:
        # Deliberately excludes commands, paths, env and payloads.
        receipt = {
            "schema": "simplicio.hub-throttle-receipt/v1",
            "client_id": client_id,
            "task_id": task_id,
            "resource": resource,
            "requested": requested,
            "available": max(0, available),
            "queue": queue,
            "reason": reason,
            "duration_ms": 0,
        }
        self._receipts.append(receipt)
        return dict(receipt)

    def admit(
        self,
        client_id: str,
        task_id: str,
        request: ResourceRequest,
        *,
        queue: str = "",
        lease_id: Optional[str] = None,
    ) -> ResourceLease:
        if not client_id or not task_id:
            raise ValueError("client_id and task_id are required")
        lease_key = lease_id or task_id
        with self._lock:
            if lease_key in self._leases:
                return self._leases[lease_key]
            now = time.monotonic()
            if self._draining:
                raise ResourceThrottled(self._receipt(
                    client_id=client_id, task_id=task_id, reason="draining", queue=queue
                ))
            if not self._circuit.allow(now):
                raise ResourceThrottled(self._receipt(
                    client_id=client_id, task_id=task_id,
                    reason="circuit_open", queue=queue
                ))
            global_used = self._usage.values
            resource = self._over_budget(global_used, request, self.limits)
            if resource:
                limit = self.limits.as_dict()[resource]
                raise ResourceThrottled(self._receipt(
                    client_id=client_id, task_id=task_id, reason="global_budget",
                    resource=resource, requested=request.as_dict()[resource],
                    available=max(0, limit - global_used[resource]), queue=queue
                ))
            client_limit = self.client_limits.get(client_id)
            client_usage = self._client_usage.setdefault(client_id, _Usage())
            if client_limit:
                resource = self._over_budget(client_usage.values, request, client_limit)
                if resource:
                    limit = client_limit.as_dict()[resource]
                    raise ResourceThrottled(self._receipt(
                        client_id=client_id, task_id=task_id, reason="client_budget",
                        resource=resource, requested=request.as_dict()[resource],
                        available=max(0, limit - client_usage.values[resource]),
                        queue=queue
                    ))
            lease = ResourceLease(
                lease_id=lease_key, client_id=client_id, task_id=task_id,
                request=request, admitted_at=now, queue=queue
            )
            self._leases[lease_key] = lease
            self._usage.add(request)
            client_usage.add(request)
            return lease

    def release(self, lease: ResourceLease) -> Dict[str, Any]:
        with self._lock:
            current = self._leases.pop(lease.lease_id, None)
            if current is None:
                return {"schema": "simplicio.hub-resource-release/v1",
                        "lease_id": lease.lease_id, "released": False}
            self._usage.subtract(current.request)
            client_usage = self._client_usage.get(current.client_id)
            if client_usage:
                client_usage.subtract(current.request)
            return {"schema": "simplicio.hub-resource-release/v1",
                    "lease_id": lease.lease_id, "released": True}

    def record_failure(self, reason: str) -> Dict[str, Any]:
        if reason not in {"oom", "thrashing", "disk_full", "gpu_pressure"}:
            raise ValueError("unsupported pressure reason")
        with self._lock:
            tripped = self._circuit.record_failure(reason)
            return {"schema": GOVERNOR_SCHEMA, "event": "pressure",
                    "reason": reason, "circuit": self._circuit.as_dict(),
                    "tripped": tripped}

    def recover(self) -> Dict[str, Any]:
        with self._lock:
            self._circuit.recover()
            return {"schema": GOVERNOR_SCHEMA, "event": "recovered",
                    "circuit": self._circuit.as_dict()}

    def drain(self) -> Dict[str, Any]:
        with self._lock:
            self._draining = True
            return self.status()

    def shutdown(self) -> Dict[str, Any]:
        with self._lock:
            self._draining = True
            leases = list(self._leases.values())
            for lease in leases:
                self.release(lease)
            return self.status()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "schema": GOVERNOR_SCHEMA,
                "draining": self._draining,
                "limits": self.limits.as_dict(),
                "used": dict(self._usage.values),
                "client_used": {
                    client: dict(usage.values)
                    for client, usage in self._client_usage.items()
                },
                "active_leases": len(self._leases),
                "throttle_receipts": len(self._receipts),
                "circuit": self._circuit.as_dict(),
            }

    def receipts(self) -> list[Dict[str, Any]]:
        with self._lock:
            return [dict(receipt) for receipt in self._receipts]
