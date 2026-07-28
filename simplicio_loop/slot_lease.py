"""Durable slot leases with heartbeats and monotonically increasing fences.

SQLite owns process coordination.  Every state transition and protected write
runs under ``BEGIN IMMEDIATE`` so two processes cannot both acquire the same
resource.  Wall-clock values are persisted for restart recovery; TTL is bounded
to prevent a skewed caller from creating an effectively infinite lease.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

LEASE_SCHEMA = "simplicio.capability-lease/v1"
RECEIPT_SCHEMA = "simplicio.capability-lease-receipt/v1"


class LeaseError(RuntimeError):
    """Base lease failure."""


class LeaseConflict(LeaseError):
    """A live lease is already owned by another worker."""


class StaleFence(LeaseError):
    """A mutation used an expired or superseded fencing token."""


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
    """Persistent, process-safe source of truth for slot ownership."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        max_ttl_seconds: float = 3600.0,
    ) -> None:
        self.path = str(Path(path))
        self.clock = clock
        self.max_ttl_seconds = float(max_ttl_seconds)
        if self.max_ttl_seconds <= 0:
            raise ValueError("max_ttl_seconds must be positive")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            # SQLite does not consistently apply busy_timeout while changing
            # journal_mode. Multiple first-start workers can therefore race
            # before the database header records WAL. Read first and retry only
            # the bounded header transition; once one worker wins, all others
            # observe WAL without issuing another write.
            deadline = time.monotonic() + 30.0
            while True:
                try:
                    current = connection.execute("PRAGMA journal_mode").fetchone()
                    if current is None or str(current[0]).lower() != "wal":
                        connection.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    detail = str(exc).lower()
                    remaining = deadline - time.monotonic()
                    if (
                        ("locked" not in detail and "busy" not in detail)
                        or remaining <= 0
                    ):
                        raise
                    time.sleep(min(0.01, remaining))
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS lease_counters (
                    resource_key TEXT PRIMARY KEY,
                    fence INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS leases (
                    resource_key TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    fence INTEGER NOT NULL,
                    issued_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS protected_values (
                    resource_key TEXT NOT NULL,
                    value_key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    fence INTEGER NOT NULL,
                    PRIMARY KEY(resource_key, value_key)
                );
                CREATE TABLE IF NOT EXISTS lease_receipts (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    resource_key TEXT NOT NULL,
                    event TEXT NOT NULL,
                    receipt_json TEXT NOT NULL
                );
                """
            )

    def _ttl(self, value: float) -> float:
        ttl = float(value)
        if ttl <= 0 or ttl > self.max_ttl_seconds:
            raise ValueError(
                f"ttl_seconds must be in (0, {self.max_ttl_seconds:g}]"
            )
        return ttl

    @staticmethod
    def _lease(row: sqlite3.Row) -> CapabilityLease:
        return CapabilityLease(**dict(row))

    def _receipt(
        self,
        connection: sqlite3.Connection,
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
        connection.execute(
            "INSERT INTO lease_receipts(resource_key,event,receipt_json) VALUES(?,?,?)",
            (lease.resource_key, event, json.dumps(body, sort_keys=True)),
        )
        return body

    def acquire(
        self, resource_key: str, owner_id: str, *, ttl_seconds: float
    ) -> dict[str, Any]:
        resource_key, owner_id = resource_key.strip(), owner_id.strip()
        if not resource_key or not owner_id:
            raise ValueError("resource_key and owner_id are required")
        ttl = self._ttl(ttl_seconds)
        now = float(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM leases WHERE resource_key=?", (resource_key,)
            ).fetchone()
            if row is not None and row["state"] == "active" and row["expires_at"] > now:
                connection.rollback()
                raise LeaseConflict(
                    f"{resource_key} held by {row['owner_id']} until {row['expires_at']}"
                )
            attempt = int(row["attempt"]) + 1 if row is not None else 1
            counter = connection.execute(
                "SELECT fence FROM lease_counters WHERE resource_key=?", (resource_key,)
            ).fetchone()
            fence = (int(counter["fence"]) if counter else 0) + 1
            connection.execute(
                """INSERT INTO lease_counters(resource_key,fence) VALUES(?,?)
                   ON CONFLICT(resource_key) DO UPDATE SET fence=excluded.fence""",
                (resource_key, fence),
            )
            lease = CapabilityLease(
                resource_key, owner_id, attempt, fence, now, now, now + ttl
            )
            connection.execute(
                """INSERT INTO leases(
                       resource_key,owner_id,attempt,fence,issued_at,
                       heartbeat_at,expires_at,state
                   ) VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(resource_key) DO UPDATE SET
                       owner_id=excluded.owner_id, attempt=excluded.attempt,
                       fence=excluded.fence, issued_at=excluded.issued_at,
                       heartbeat_at=excluded.heartbeat_at,
                       expires_at=excluded.expires_at, state=excluded.state""",
                (
                    lease.resource_key, lease.owner_id, lease.attempt, lease.fence,
                    lease.issued_at, lease.heartbeat_at, lease.expires_at, lease.state,
                ),
            )
            receipt = self._receipt(connection, "acquired", lease, observed_at=now)
            connection.commit()
            return {"lease": lease.to_dict(), "receipt": receipt}

    def _require_current(
        self,
        connection: sqlite3.Connection,
        resource_key: str,
        owner_id: str,
        fence: int,
        now: float,
    ) -> CapabilityLease:
        row = connection.execute(
            "SELECT * FROM leases WHERE resource_key=?", (resource_key,)
        ).fetchone()
        if row is None:
            raise StaleFence("resource has no lease")
        lease = self._lease(row)
        if lease.state != "active" or lease.expires_at <= now:
            if lease.state == "active":
                connection.execute(
                    "UPDATE leases SET state='stale' WHERE resource_key=? AND fence=?",
                    (resource_key, lease.fence),
                )
            raise StaleFence("lease expired")
        if lease.owner_id != owner_id or lease.fence != int(fence):
            raise StaleFence("owner or fencing token is stale")
        return lease

    def heartbeat(
        self,
        resource_key: str,
        owner_id: str,
        fence: int,
        *,
        ttl_seconds: float,
    ) -> dict[str, Any]:
        ttl, now = self._ttl(ttl_seconds), float(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = self._require_current(
                connection, resource_key, owner_id, fence, now
            )
            renewed = CapabilityLease(
                lease.resource_key, lease.owner_id, lease.attempt, lease.fence,
                lease.issued_at, now, now + ttl,
            )
            connection.execute(
                """UPDATE leases SET heartbeat_at=?, expires_at=?
                   WHERE resource_key=? AND fence=?""",
                (renewed.heartbeat_at, renewed.expires_at, resource_key, fence),
            )
            receipt = self._receipt(
                connection, "heartbeat", renewed, observed_at=now
            )
            connection.commit()
            return {"lease": renewed.to_dict(), "receipt": receipt}

    def put(
        self,
        resource_key: str,
        owner_id: str,
        fence: int,
        value_key: str,
        value: Any,
    ) -> dict[str, Any]:
        """Atomically fence and persist a protected mutation."""
        now = float(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = self._require_current(
                connection, resource_key, owner_id, fence, now
            )
            connection.execute(
                """INSERT INTO protected_values(resource_key,value_key,value_json,fence)
                   VALUES(?,?,?,?)
                   ON CONFLICT(resource_key,value_key) DO UPDATE SET
                       value_json=excluded.value_json,fence=excluded.fence""",
                (resource_key, value_key, json.dumps(value, sort_keys=True), fence),
            )
            receipt = self._receipt(
                connection, "mutation_committed", lease, observed_at=now
            )
            connection.commit()
            return {"value_key": value_key, "fence": fence, "receipt": receipt}

    def read(self, resource_key: str, value_key: str) -> Any:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT value_json FROM protected_values
                   WHERE resource_key=? AND value_key=?""",
                (resource_key, value_key),
            ).fetchone()
        return None if row is None else json.loads(row["value_json"])

    def release(
        self, resource_key: str, owner_id: str, fence: int
    ) -> dict[str, Any]:
        now = float(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = self._require_current(
                connection, resource_key, owner_id, fence, now
            )
            released = CapabilityLease(
                lease.resource_key, lease.owner_id, lease.attempt, lease.fence,
                lease.issued_at, lease.heartbeat_at, lease.expires_at, "released",
            )
            connection.execute(
                "UPDATE leases SET state='released' WHERE resource_key=? AND fence=?",
                (resource_key, fence),
            )
            receipt = self._receipt(
                connection, "released", released, observed_at=now
            )
            connection.commit()
            return {"lease": released.to_dict(), "receipt": receipt}

    def mark_stale(self) -> list[dict[str, Any]]:
        now = float(self.clock())
        receipts: list[dict[str, Any]] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM leases WHERE state='active' AND expires_at<=?",
                (now,),
            ).fetchall()
            for row in rows:
                lease = self._lease(row)
                stale = CapabilityLease(
                    lease.resource_key, lease.owner_id, lease.attempt, lease.fence,
                    lease.issued_at, lease.heartbeat_at, lease.expires_at, "stale",
                )
                connection.execute(
                    "UPDATE leases SET state='stale' WHERE resource_key=? AND fence=?",
                    (lease.resource_key, lease.fence),
                )
                receipts.append(
                    self._receipt(
                        connection, "marked_stale", stale, observed_at=now,
                        reason="heartbeat_expired",
                    )
                )
            connection.commit()
        return receipts

    def reclaim(
        self, resource_key: str, new_owner_id: str, *, ttl_seconds: float
    ) -> dict[str, Any]:
        """Reclaim only an expired/stale/released lease with a new attempt/fence."""
        self.mark_stale()
        return self.acquire(resource_key, new_owner_id, ttl_seconds=ttl_seconds)

    def status(self, resource_key: str) -> dict[str, Any] | None:
        now = float(self.clock())
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM leases WHERE resource_key=?", (resource_key,)
            ).fetchone()
        if row is None:
            return None
        lease = self._lease(row)
        effective = "stale" if lease.state == "active" and lease.expires_at <= now else lease.state
        return {
            **lease.to_dict(),
            "state": effective,
            "age_seconds": max(0.0, now - lease.issued_at),
            "expires_in_seconds": max(0.0, lease.expires_at - now),
            "conflict": effective == "active",
        }

    def receipts(self, resource_key: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT receipt_json FROM lease_receipts
                   WHERE resource_key=? ORDER BY sequence""",
                (resource_key,),
            ).fetchall()
        return [json.loads(row["receipt_json"]) for row in rows]
