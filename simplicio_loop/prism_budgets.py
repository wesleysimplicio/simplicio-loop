"""Adaptive Prism budgets and fenced multi-device rebalancing."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .hbp_ledger import canonical_sha256
from .prism_scheduler import BudgetObservation, PrismPolicy, ResourceVector

BUDGET_STATUS_SCHEMA = "simplicio.prism-budget-status/v1"
DEVICE_REBALANCE_SCHEMA = "simplicio.prism-device-rebalance/v1"
THROUGHPUT_SCHEMA = "simplicio.prism-throughput/v1"
RESOURCE_NAMES = tuple(field.name for field in dataclasses.fields(ResourceVector))


class PrismBudgetError(RuntimeError):
    """A budget or lease change cannot be applied safely."""

    reason_code = "PRISM_BUDGET_ERROR"


def _bounded_int(value: int | None, name: str, *, minimum: int = 0) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PrismBudgetError(f"{name} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True)
class DeviceCapacity:
    device_id: str
    worker_limit: int
    trusted: bool = True
    connected: bool = True
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.device_id:
            raise PrismBudgetError("device_id is required")
        _bounded_int(self.worker_limit, "worker_limit", minimum=1)
        object.__setattr__(
            self,
            "capabilities",
            tuple(sorted({item for item in self.capabilities if item})),
        )


@dataclass(frozen=True)
class BudgetSample:
    """One observed control-plane sample; unknown values stay explicit."""

    workers: int | None = None
    cpu_millis: int | None = None
    rss_bytes: int | None = None
    io_units: int | None = None
    provider_requests: int | None = None
    tokens: int | None = None
    disk_bytes: int | None = None
    model_slots: int | None = None
    network_queue: int | None = None
    context_tokens: int | None = None
    evidence_bytes: int | None = None
    provider_retry_after_ns: int | None = None
    observed_at_ns: int = 0
    null_reasons: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in RESOURCE_NAMES:
            _bounded_int(getattr(self, name), name)
        _bounded_int(self.provider_retry_after_ns, "provider_retry_after_ns")
        _bounded_int(self.observed_at_ns, "observed_at_ns")
        invalid = set(self.null_reasons) - set(RESOURCE_NAMES)
        if invalid:
            raise PrismBudgetError(f"unknown null_reason dimensions: {sorted(invalid)}")
        for name, reason in self.null_reasons.items():
            if getattr(self, name) is not None or not str(reason).strip():
                raise PrismBudgetError(
                    f"null_reason for {name} requires an unknown metric"
                )

    def observation(
        self,
        policy: PrismPolicy,
        devices: Sequence[DeviceCapacity] = (),
    ) -> BudgetObservation:
        connected = [
            device
            for device in devices
            if device.connected and device.trusted and device.worker_limit
        ]
        device_workers = sum(device.worker_limit for device in connected)
        configured_workers = self.workers or policy.global_worker_limit
        if devices:
            configured_workers = min(configured_workers, max(1, device_workers))

        values: dict[str, int] = {}
        measured: list[str] = []
        unavailable: list[str] = []
        null_reasons = dict(self.null_reasons)
        for name in RESOURCE_NAMES:
            value = getattr(self, name)
            if name == "workers":
                value = configured_workers
            if value is None:
                unavailable.append(name)
                null_reasons.setdefault(name, "metric_not_observed")
                # A non-zero conservative bound avoids the ResourceVector
                # convention where zero means "not constrained".
                value = 1
            else:
                measured.append(name)
            values[name] = value
        return BudgetObservation(
            limit=ResourceVector(**values),
            measured=tuple(measured),
            unavailable=tuple(unavailable),
            null_reasons=null_reasons,
            provider_retry_after_ns=self.provider_retry_after_ns,
            observed_at_ns=self.observed_at_ns,
            device_workers=tuple(
                sorted((device.device_id, device.worker_limit) for device in connected)
            ),
        )


class AdaptiveBudgetGovernor:
    """Apply pressure immediately and capacity relief after stable samples."""

    def __init__(
        self,
        policy: PrismPolicy,
        *,
        relief_samples: int = 2,
        devices: Sequence[DeviceCapacity] = (),
    ) -> None:
        if isinstance(relief_samples, bool) or relief_samples < 1:
            raise PrismBudgetError("relief_samples must be positive")
        self.policy = policy
        self.relief_samples = relief_samples
        self.devices = {device.device_id: device for device in devices}
        self.current: BudgetObservation | None = None
        self._candidate: BudgetObservation | None = None
        self._relief_streak = 0
        self._events: list[dict[str, Any]] = []

    @staticmethod
    def _limits(observation: BudgetObservation) -> tuple[int, ...]:
        return tuple(getattr(observation.limit, name) for name in RESOURCE_NAMES)

    def observe(self, sample: BudgetSample) -> BudgetObservation:
        observed = sample.observation(self.policy, tuple(self.devices.values()))
        reason = "INITIAL_SAMPLE"
        applied = True
        if self.current is not None:
            current = self._limits(self.current)
            proposed = self._limits(observed)
            pressure = any(new < old for old, new in zip(current, proposed))
            provider_pressure = (
                observed.provider_retry_after_ns is not None
                and observed.provider_retry_after_ns
                != self.current.provider_retry_after_ns
            )
            if pressure or provider_pressure:
                reason = "PRESSURE_APPLIED"
                self._candidate = None
                self._relief_streak = 0
            elif proposed == current:
                reason = "STABLE"
                self._candidate = None
                self._relief_streak = 0
            else:
                if self._candidate == observed:
                    self._relief_streak += 1
                else:
                    self._candidate = observed
                    self._relief_streak = 1
                if self._relief_streak < self.relief_samples:
                    reason = "RELIEF_HYSTERESIS"
                    applied = False
                else:
                    reason = "RELIEF_APPLIED"
                    self._candidate = None
                    self._relief_streak = 0
        if applied:
            self.current = observed
        event = {
            "applied": applied,
            "reason_code": reason,
            "observed_at_ns": observed.observed_at_ns,
            "limits": dataclasses.asdict(observed.limit),
            "unavailable": list(observed.unavailable),
            "null_reasons": dict(observed.null_reasons),
        }
        event["event_hash"] = canonical_sha256(event)
        self._events.append(event)
        return self.current or observed

    def update_device(self, device: DeviceCapacity) -> None:
        self.devices[device.device_id] = device

    def status(self) -> dict[str, Any]:
        payload = {
            "schema": BUDGET_STATUS_SCHEMA,
            "current": (
                dataclasses.asdict(self.current.limit) if self.current else None
            ),
            "devices": [
                dataclasses.asdict(device)
                for device in sorted(
                    self.devices.values(), key=lambda item: item.device_id
                )
            ],
            "events": list(self._events),
            "relief_streak": self._relief_streak,
        }
        payload["status_hash"] = canonical_sha256(payload)
        return payload


@dataclass(frozen=True)
class DeviceLease:
    task_id: str
    device_id: str
    fence: int
    capability: str

    def __post_init__(self) -> None:
        if not self.task_id or not self.device_id or not self.capability:
            raise PrismBudgetError("lease identity and capability are required")
        _bounded_int(self.fence, "fence", minimum=1)


class DeviceLeaseLedger:
    """One current fenced lease per task, even across device loss."""

    def __init__(self, devices: Sequence[DeviceCapacity]) -> None:
        self.devices = {device.device_id: device for device in devices}
        if len(self.devices) != len(devices):
            raise PrismBudgetError("duplicate device")
        self.leases: dict[str, DeviceLease] = {}
        self._served = {device.device_id: 0 for device in devices}

    def assign(self, task_id: str, capability: str) -> DeviceLease:
        if task_id in self.leases:
            raise PrismBudgetError("task already has a current device lease")
        candidates = self._candidates(capability)
        if not candidates:
            raise PrismBudgetError("DEVICE_CAPABILITY_UNAVAILABLE")
        device = min(candidates, key=lambda item: (self._served[item.device_id], item.device_id))
        lease = DeviceLease(task_id, device.device_id, 1, capability)
        self.leases[task_id] = lease
        self._served[device.device_id] += 1
        return lease

    def disconnect(self, device_id: str) -> list[dict[str, Any]]:
        device = self.devices.get(device_id)
        if device is None:
            raise PrismBudgetError("unknown device")
        self.devices[device_id] = dataclasses.replace(device, connected=False)
        actions: list[dict[str, Any]] = []
        for task_id, lease in sorted(self.leases.items()):
            if lease.device_id != device_id:
                continue
            candidates = self._candidates(lease.capability)
            if not candidates:
                action = {
                    "schema": DEVICE_REBALANCE_SCHEMA,
                    "task_id": task_id,
                    "from_device": device_id,
                    "to_device": None,
                    "old_fence": lease.fence,
                    "new_fence": None,
                    "reason_code": "DEVICE_LOST_RECOVERY_REQUIRED",
                    "work_duplicated": False,
                }
            else:
                target = min(
                    candidates,
                    key=lambda item: (self._served[item.device_id], item.device_id),
                )
                replacement = dataclasses.replace(
                    lease,
                    device_id=target.device_id,
                    fence=lease.fence + 1,
                )
                self.leases[task_id] = replacement
                self._served[target.device_id] += 1
                action = {
                    "schema": DEVICE_REBALANCE_SCHEMA,
                    "task_id": task_id,
                    "from_device": device_id,
                    "to_device": target.device_id,
                    "old_fence": lease.fence,
                    "new_fence": replacement.fence,
                    "reason_code": "DEVICE_LOST_REBALANCED",
                    "work_duplicated": False,
                }
            action["action_hash"] = canonical_sha256(action)
            actions.append(action)
        return actions

    def _candidates(self, capability: str) -> list[DeviceCapacity]:
        return [
            device
            for device in self.devices.values()
            if device.connected
            and device.trusted
            and capability in device.capabilities
            and sum(
                lease.device_id == device.device_id for lease in self.leases.values()
            )
            < device.worker_limit
        ]

    def assert_current(self, lease: DeviceLease) -> None:
        if self.leases.get(lease.task_id) != lease:
            raise PrismBudgetError("STALE_DEVICE_FENCE")


def throughput_receipt(
    *,
    verified_tasks: int,
    elapsed_ns: int,
    token_count: int | None,
    cost_units: int | None,
) -> dict[str, Any]:
    """Publish observed throughput and cost without inventing missing metrics."""
    _bounded_int(verified_tasks, "verified_tasks")
    _bounded_int(elapsed_ns, "elapsed_ns")
    _bounded_int(token_count, "token_count")
    _bounded_int(cost_units, "cost_units")
    payload = {
        "schema": THROUGHPUT_SCHEMA,
        "verified_tasks": verified_tasks,
        "elapsed_ns": elapsed_ns,
        "throughput_tasks_per_second_milli": (
            verified_tasks * 1_000_000_000_000 // elapsed_ns
            if elapsed_ns and verified_tasks
            else 0
        ),
        "token_count": token_count,
        "tokens_per_verified_task_milli": (
            token_count * 1_000 // verified_tasks
            if token_count is not None and verified_tasks
            else None
        ),
        "cost_units": cost_units,
        "cost_units_per_verified_task_milli": (
            cost_units * 1_000 // verified_tasks
            if cost_units is not None and verified_tasks
            else None
        ),
        "null_reasons": {
            key: reason
            for key, value, reason in (
                (
                    "tokens_per_verified_task_milli",
                    token_count,
                    "token_count_not_observed",
                ),
                (
                    "cost_units_per_verified_task_milli",
                    cost_units,
                    "cost_not_observed",
                ),
            )
            if value is None
        },
    }
    payload["receipt_hash"] = canonical_sha256(payload)
    return payload


__all__ = [
    "BUDGET_STATUS_SCHEMA",
    "DEVICE_REBALANCE_SCHEMA",
    "THROUGHPUT_SCHEMA",
    "AdaptiveBudgetGovernor",
    "BudgetSample",
    "DeviceCapacity",
    "DeviceLease",
    "DeviceLeaseLedger",
    "PrismBudgetError",
    "throughput_receipt",
]
