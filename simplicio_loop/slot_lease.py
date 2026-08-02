"""Durable resource leases projected from the MapperStore operations journal.

Loop owns lease policy and receipt semantics; Mapper owns the durable SQLite
authority, hash chain, and cross-process compare-and-swap boundary.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .mapper_operations import MapperOperationsAdapter

LEASE_SCHEMA = "simplicio.capability-lease/v1"
RECEIPT_SCHEMA = "simplicio.capability-lease-receipt/v1"
EVENT_SCHEMA = "simplicio.loop-resource-lease-event/v1"
JOURNAL_PREFIX = "simplicio.loop.resource-fabric:"


class LeaseError(RuntimeError):
    """Base lease failure."""


class LeaseConflict(LeaseError):
    """A live lease is already owned by another worker."""


class StaleFence(LeaseError):
    """A mutation used an expired or superseded fencing token."""


class _JournalConflict(LeaseError):
    """The projection changed before this mutation committed."""


@dataclass(frozen=True)
class CapabilityLease:
    resource_key: str
    owner_id: str
    attempt: int
    fence: int
    issued_at: float
    heartbeat_at: float
    expires_at: float
    state: str = "active"
    schema: str = LEASE_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _digest(value: dict[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class LeaseStore:
    """Persistent, process-safe source of truth for resource ownership."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        max_ttl_seconds: float = 3600.0,
        operations: Any | None = None,
    ) -> None:
        self.path = str(Path(path))
        self.clock = clock
        self.max_ttl_seconds = float(max_ttl_seconds)
        if self.max_ttl_seconds <= 0:
            raise ValueError("max_ttl_seconds must be positive")
        self._operations = operations or MapperOperationsAdapter(self.path)
        self._journal_id = JOURNAL_PREFIX + self.path
        self._operations.initialize()

    @staticmethod
    def _last_seq(replay: Mapping[str, Any]) -> int:
        events = replay.get("events") or []
        if events:
            return int(events[-1]["seq"])
        compaction = replay.get("compaction")
        return int(compaction["through_seq"]) if compaction else 0

    def _replay(self) -> dict[str, Any]:
        replay = self._operations.replay(self._journal_id)
        if not replay.get("valid", False):
            raise LeaseError("lease journal is invalid")
        return replay

    def _state(self, replay: Mapping[str, Any]) -> dict[str, Any]:
        state: dict[str, Any] = {
            "leases": {},
            "counters": {},
            "protected": {},
            "receipts": [],
        }
        for event in replay.get("events", []):
            payload = event.get("payload")
            if not isinstance(payload, Mapping) or payload.get("schema") != EVENT_SCHEMA:
                raise LeaseError("lease journal contains an unknown event")
            operation = payload.get("operation")
            resource_key = str(payload.get("resource_key", ""))
            if operation in {"acquired", "heartbeat", "released", "marked_stale", "invalidated"}:
                lease = dict(payload["lease"])
                state["leases"][resource_key] = lease
                state["counters"][resource_key] = max(
                    int(state["counters"].get(resource_key, 0)), int(lease["fence"])
                )
            elif operation == "mutation_committed":
                values = state["protected"].setdefault(resource_key, {})
                values[str(payload["value_key"])] = {
                    "value": payload.get("value"),
                    "fence": int(payload["fence"]),
                }
            else:
                raise LeaseError("lease journal contains an unknown operation")
            receipt = payload.get("receipt")
            if isinstance(receipt, Mapping):
                state["receipts"].append(dict(receipt))
        return state

    @staticmethod
    def _is_conflict(error: BaseException) -> bool:
        return "JOURNAL_CONFLICT" in str(error) or getattr(error, "reason_code", "") == "JOURNAL_CONFLICT"

    def _append(
        self,
        replay: Mapping[str, Any],
        operation: str,
        resource_key: str,
        payload: Mapping[str, Any],
    ) -> None:
        body = {
            "schema": EVENT_SCHEMA,
            "operation": operation,
            "resource_key": resource_key,
            **dict(payload),
        }
        try:
            self._operations.append_event(
                self._journal_id,
                "resource_lease." + operation,
                body,
                expected_seq=self._last_seq(replay),
            )
        except Exception as error:
            if self._is_conflict(error):
                raise _JournalConflict() from error
            raise LeaseError("lease journal append failed: " + str(error)) from error

    def _ttl(self, value: float) -> float:
        ttl = float(value)
        if ttl <= 0 or ttl > self.max_ttl_seconds:
            raise ValueError(f"ttl_seconds must be in (0, {self.max_ttl_seconds:g}]")
        return ttl

    @staticmethod
    def _lease(value: Mapping[str, Any]) -> CapabilityLease:
        return CapabilityLease(**dict(value))

    @staticmethod
    def _receipt(
        event: str,
        lease: CapabilityLease,
        *,
        observed_at: float,
        reason: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema": RECEIPT_SCHEMA,
            "event": event,
            "observed_at": observed_at,
            "resource_key": lease.resource_key,
            "owner_id": lease.owner_id,
            "attempt": lease.attempt,
            "fence": lease.fence,
            "expires_at": lease.expires_at,
            "state": lease.state,
            "reason": reason,
        }
        body["receipt_hash"] = _digest(body)
        return body

    @staticmethod
    def _effective(lease: Mapping[str, Any], now: float) -> dict[str, Any]:
        state = str(lease["state"])
        if state == "active" and float(lease["expires_at"]) <= now:
            state = "stale"
        return {
            **dict(lease),
            "state": state,
            "expires_in_seconds": max(0.0, float(lease["expires_at"]) - now),
            "conflict": state == "active",
        }

    def _require_current(
        self,
        state: Mapping[str, Any],
        resource_key: str,
        owner_id: str,
        fence: int,
        now: float,
    ) -> CapabilityLease:
        raw = state["leases"].get(resource_key)
        if raw is None:
            raise StaleFence("resource has no lease")
        lease = self._lease(raw)
        if lease.state != "active" or lease.expires_at <= now:
            raise StaleFence("lease expired")
        if lease.owner_id != owner_id or lease.fence != int(fence):
            raise StaleFence("owner or fencing token is stale")
        return lease

    def acquire(self, resource_key: str, owner_id: str, *, ttl_seconds: float) -> dict[str, Any]:
        resource_key, owner_id = resource_key.strip(), owner_id.strip()
        if not resource_key or not owner_id:
            raise ValueError("resource_key and owner_id are required")
        ttl = self._ttl(ttl_seconds)
        for _ in range(32):
            replay = self._replay()
            state = self._state(replay)
            current = state["leases"].get(resource_key)
            now = float(self.clock())
            if current is not None and current["state"] == "active" and float(current["expires_at"]) > now:
                raise LeaseConflict(f"{resource_key} held by {current['owner_id']} until {current['expires_at']}")
            attempt = int(current["attempt"]) + 1 if current is not None else 1
            fence = int(state["counters"].get(resource_key, 0)) + 1
            lease = CapabilityLease(resource_key, owner_id, attempt, fence, now, now, now + ttl)
            receipt = self._receipt("acquired", lease, observed_at=now)
            try:
                self._append(replay, "acquired", resource_key, {"lease": lease.to_dict(), "receipt": receipt})
                return {"lease": lease.to_dict(), "receipt": receipt}
            except _JournalConflict:
                continue
        raise LeaseError("lease acquisition remained contended")

    def heartbeat(
        self,
        resource_key: str,
        owner_id: str,
        fence: int,
        *,
        ttl_seconds: float,
    ) -> dict[str, Any]:
        ttl = self._ttl(ttl_seconds)
        for _ in range(32):
            replay = self._replay()
            state = self._state(replay)
            now = float(self.clock())
            lease = self._require_current(state, resource_key, owner_id, fence, now)
            renewed = CapabilityLease(
                lease.resource_key, lease.owner_id, lease.attempt, lease.fence,
                lease.issued_at, now, now + ttl,
            )
            receipt = self._receipt("heartbeat", renewed, observed_at=now)
            try:
                self._append(replay, "heartbeat", resource_key, {"lease": renewed.to_dict(), "receipt": receipt})
                return {"lease": renewed.to_dict(), "receipt": receipt}
            except _JournalConflict:
                continue
        raise LeaseError("lease heartbeat remained contended")

    def put(
        self,
        resource_key: str,
        owner_id: str,
        fence: int,
        value_key: str,
        value: Any,
    ) -> dict[str, Any]:
        """Atomically fence and persist a protected mutation."""
        for _ in range(32):
            replay = self._replay()
            state = self._state(replay)
            now = float(self.clock())
            lease = self._require_current(state, resource_key, owner_id, fence, now)
            receipt = self._receipt("mutation_committed", lease, observed_at=now)
            try:
                self._append(
                    replay,
                    "mutation_committed",
                    resource_key,
                    {"value_key": value_key, "value": value, "fence": int(fence), "receipt": receipt},
                )
                return {"value_key": value_key, "fence": int(fence), "receipt": receipt}
            except _JournalConflict:
                continue
        raise LeaseError("protected mutation remained contended")

    def read(self, resource_key: str, value_key: str) -> Any:
        state = self._state(self._replay())
        value = state["protected"].get(resource_key, {}).get(value_key)
        return None if value is None else value["value"]

    def release(self, resource_key: str, owner_id: str, fence: int) -> dict[str, Any]:
        for _ in range(32):
            replay = self._replay()
            state = self._state(replay)
            now = float(self.clock())
            lease = self._require_current(state, resource_key, owner_id, fence, now)
            released = CapabilityLease(
                lease.resource_key, lease.owner_id, lease.attempt, lease.fence,
                lease.issued_at, lease.heartbeat_at, lease.expires_at, "released",
            )
            receipt = self._receipt("released", released, observed_at=now)
            try:
                self._append(replay, "released", resource_key, {"lease": released.to_dict(), "receipt": receipt})
                return {"lease": released.to_dict(), "receipt": receipt}
            except _JournalConflict:
                continue
        raise LeaseError("lease release remained contended")

    def mark_stale(self) -> list[dict[str, Any]]:
        receipts: list[dict[str, Any]] = []
        while True:
            replay = self._replay()
            state = self._state(replay)
            now = float(self.clock())
            candidate = next(
                (lease for lease in state["leases"].values()
                 if lease["state"] == "active" and float(lease["expires_at"]) <= now),
                None,
            )
            if candidate is None:
                return receipts
            lease = self._lease(candidate)
            stale = CapabilityLease(
                lease.resource_key, lease.owner_id, lease.attempt, lease.fence,
                lease.issued_at, lease.heartbeat_at, lease.expires_at, "stale",
            )
            receipt = self._receipt("marked_stale", stale, observed_at=now, reason="heartbeat_expired")
            try:
                self._append(replay, "marked_stale", lease.resource_key, {"lease": stale.to_dict(), "receipt": receipt})
                receipts.append(receipt)
            except _JournalConflict:
                continue

    def reclaim(self, resource_key: str, new_owner_id: str, *, ttl_seconds: float) -> dict[str, Any]:
        """Reclaim only an expired/stale/released lease with a new attempt/fence."""
        self.mark_stale()
        return self.acquire(resource_key, new_owner_id, ttl_seconds=ttl_seconds)

    def invalidate(self, resource_key: str, *, reason: str = "invalidated") -> dict[str, Any] | None:
        for _ in range(32):
            replay = self._replay()
            state = self._state(replay)
            raw = state["leases"].get(resource_key)
            if raw is None:
                return None
            now = float(self.clock())
            lease = self._lease(raw)
            stale = CapabilityLease(
                lease.resource_key, lease.owner_id, lease.attempt, lease.fence,
                lease.issued_at, lease.heartbeat_at, lease.expires_at,
                "stale" if lease.state == "active" else lease.state,
            )
            receipt = self._receipt("invalidated", stale, observed_at=now, reason=reason)
            try:
                self._append(replay, "invalidated", resource_key, {"lease": stale.to_dict(), "receipt": receipt})
                return {"lease": stale.to_dict(), "receipt": receipt}
            except _JournalConflict:
                continue
        raise LeaseError("lease invalidation remained contended")

    def invalidate_owner(self, owner_id: str, *, reason: str = "authority_takeover") -> list[dict[str, Any]]:
        owner_id = str(owner_id).strip()
        if not owner_id:
            raise ValueError("owner_id is required")
        receipts: list[dict[str, Any]] = []
        while True:
            replay = self._replay()
            state = self._state(replay)
            candidate = next(
                (lease for lease in state["leases"].values()
                 if lease["owner_id"] == owner_id and lease["state"] == "active"),
                None,
            )
            if candidate is None:
                return receipts
            lease = self._lease(candidate)
            stale = CapabilityLease(
                lease.resource_key, lease.owner_id, lease.attempt, lease.fence,
                lease.issued_at, lease.heartbeat_at, lease.expires_at, "stale",
            )
            receipt = self._receipt("invalidated", stale, observed_at=float(self.clock()), reason=reason)
            try:
                self._append(replay, "invalidated", lease.resource_key, {"lease": stale.to_dict(), "receipt": receipt})
                receipts.append(receipt)
            except _JournalConflict:
                continue

    def list_leases(self, *, prefix: str = "") -> list[dict[str, Any]]:
        """Read observable lease state for a coordinator/doctor without writing."""
        now = float(self.clock())
        state = self._state(self._replay())
        return [
            self._effective(lease, now)
            for resource_key, lease in sorted(state["leases"].items())
            if resource_key.startswith(prefix)
        ]

    def status(self, resource_key: str) -> dict[str, Any] | None:
        now = float(self.clock())
        state = self._state(self._replay())
        lease = state["leases"].get(resource_key)
        if lease is None:
            return None
        effective = self._effective(lease, now)
        return {
            **effective,
            "age_seconds": max(0.0, now - float(lease["issued_at"])),
        }

    def receipts(self, resource_key: str) -> list[dict[str, Any]]:
        state = self._state(self._replay())
        return [receipt for receipt in state["receipts"] if receipt.get("resource_key") == resource_key]
