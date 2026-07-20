"""Shared run budget and delta/context-pack primitives.

The budget is deliberately small and stdlib-only so every runtime adapter can use the
same durable control-plane contract.  SQLite's ``BEGIN IMMEDIATE`` makes admission
atomic across processes (and therefore across local worker processes), while the
reservation/settlement keys make retries and late receipts idempotent.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple, Union

RUN_BUDGET_SCHEMA = "simplicio.run-budget/v1"
RESERVATION_SCHEMA = "simplicio.budget-reservation/v1"
SETTLEMENT_SCHEMA = "simplicio.usage-settlement/v1"
CONTEXT_PACK_SCHEMA = "simplicio.context-pack-ref/v1"
DELTA_SCHEMA = "simplicio.continuation-delta/v1"


class BudgetError(RuntimeError):
    """Base error for fail-closed budget operations."""


class BudgetExceeded(BudgetError):
    """The configured envelope cannot admit or settle the requested usage."""


class UnknownReservation(BudgetError):
    """A settlement/cancellation referred to no known reservation."""


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _now() -> float:
    return time.time()


@dataclass(frozen=True)
class RunBudget:
    run_id: str
    token_limit: int
    call_limit: int = 0
    cost_limit_micros: int = 0
    latency_limit_ms: int = 0
    exhaustion_policy: str = "stop"

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        for name in ("token_limit", "call_limit", "cost_limit_micros", "latency_limit_ms"):
            if getattr(self, name) < 0:
                raise ValueError(name + " must be non-negative")
        if self.exhaustion_policy not in ("stop", "compress", "serial", "downgrade", "escalate"):
            raise ValueError("unsupported exhaustion policy")

    def as_dict(self) -> Dict[str, Any]:
        return {"schema": RUN_BUDGET_SCHEMA, "run_id": self.run_id,
                "token_limit": self.token_limit, "call_limit": self.call_limit,
                "cost_limit_micros": self.cost_limit_micros,
                "latency_limit_ms": self.latency_limit_ms,
                "exhaustion_policy": self.exhaustion_policy}


class BudgetLedger:
    """Durable, cross-process run budget ledger backed by SQLite."""

    def __init__(self, path: Union[str, Path], budget: RunBudget):
        self.path = str(path)
        self.budget = budget
        self._lock = threading.RLock()
        with self._connect() as db:
            # Serialize schema discovery, migration and envelope creation in a
            # single SQLite transaction.  DDL is transactional when issued as
            # individual statements; ``executescript`` would implicitly commit
            # and reopen the rename/create race between constructors.
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("""CREATE TABLE IF NOT EXISTS budget_runs (
                    run_id TEXT PRIMARY KEY, envelope TEXT NOT NULL,
                    spent_tokens INTEGER NOT NULL DEFAULT 0,
                    spent_calls INTEGER NOT NULL DEFAULT 0,
                    spent_cost INTEGER NOT NULL DEFAULT 0,
                    spent_latency INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL)""")
                db.execute("""CREATE TABLE IF NOT EXISTS budget_reservations (
                    reservation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                    work_item_id TEXT NOT NULL, estimate_tokens INTEGER NOT NULL,
                    estimate_calls INTEGER NOT NULL, estimate_cost INTEGER NOT NULL,
                    estimate_latency INTEGER NOT NULL, state TEXT NOT NULL,
                    created_at REAL NOT NULL, expires_at REAL)""")
                db.execute("""CREATE TABLE IF NOT EXISTS budget_settlements (
                    reservation_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    payload TEXT NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY (run_id, reservation_id))""")
                self._migrate_settlement_key(db)
                # Concurrent workers can construct the same durable ledger at once. The
                # idempotent insert makes envelope initialization atomic at SQLite's
                # uniqueness boundary; the read below still rejects drift.
                db.execute(
                    "INSERT OR IGNORE INTO budget_runs(run_id,envelope,created_at) VALUES(?,?,?)",
                    (budget.run_id, _json(budget.as_dict()), _now()),
                )
                existing = db.execute(
                    "SELECT envelope FROM budget_runs WHERE run_id=?", (budget.run_id,),
                ).fetchone()
                if json.loads(existing[0]) != budget.as_dict():
                    raise BudgetError("run budget is immutable after freeze")
            except Exception:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    @staticmethod
    def _migrate_settlement_key(db: sqlite3.Connection) -> None:
        """Upgrade legacy global receipt ids to run-scoped idempotency keys."""
        columns = db.execute("PRAGMA table_info(budget_settlements)").fetchall()
        primary_key = [row["name"] for row in sorted(columns, key=lambda row: row["pk"]) if row["pk"]]
        if primary_key == ["run_id", "reservation_id"]:
            # Recover a receipt table stranded by the former non-transactional
            # migration before dropping the backup.
            legacy = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='budget_settlements_legacy'",
            ).fetchone()
            if legacy:
                db.execute(
                    "INSERT OR IGNORE INTO budget_settlements(reservation_id,run_id,payload,created_at) "
                    "SELECT reservation_id,run_id,payload,created_at FROM budget_settlements_legacy",
                )
                db.execute("DROP TABLE budget_settlements_legacy")
            return
        # SQLite cannot alter a primary key in place.  A legacy table has one
        # globally unique receipt per id, so copying it into the composite key
        # table preserves every receipt while allowing independent later runs.
        db.execute("ALTER TABLE budget_settlements RENAME TO budget_settlements_legacy")
        db.execute("""CREATE TABLE budget_settlements (
                reservation_id TEXT NOT NULL, run_id TEXT NOT NULL,
                payload TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY (run_id, reservation_id))"""
        )
        db.execute(
            "INSERT INTO budget_settlements(reservation_id,run_id,payload,created_at) "
            "SELECT reservation_id,run_id,payload,created_at FROM budget_settlements_legacy",
        )
        db.execute("DROP TABLE budget_settlements_legacy")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _totals(self, db: sqlite3.Connection) -> Tuple[int, int, int, int, int, int, int, int]:
        run = db.execute("SELECT * FROM budget_runs WHERE run_id=?", (self.budget.run_id,)).fetchone()
        reserved = db.execute("SELECT COALESCE(SUM(estimate_tokens),0), COALESCE(SUM(estimate_calls),0), COALESCE(SUM(estimate_cost),0), COALESCE(SUM(estimate_latency),0) FROM budget_reservations WHERE run_id=? AND state='reserved'", (self.budget.run_id,)).fetchone()
        return (int(run["spent_tokens"]), int(run["spent_calls"]), int(run["spent_cost"]), int(run["spent_latency"]),
                int(reserved[0]), int(reserved[1]), int(reserved[2]), int(reserved[3]))

    def reserve(self, reservation_id: str, work_item_id: str, *, tokens: int, calls: int = 1,
                cost_micros: int = 0, latency_ms: int = 0, expires_at: Optional[float] = None) -> Dict[str, Any]:
        values = (tokens, calls, cost_micros, latency_ms)
        if not reservation_id.strip() or not work_item_id.strip() or any(v < 0 for v in values):
            raise ValueError("reservation ids and estimates must be valid")
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM budget_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
            if prior is not None:
                if (prior["run_id"], prior["work_item_id"], prior["estimate_tokens"], prior["estimate_calls"], prior["estimate_cost"], prior["estimate_latency"]) != (self.budget.run_id, work_item_id, tokens, calls, cost_micros, latency_ms):
                    raise BudgetError("reservation id reused with different estimate")
                return dict(prior)
            spent_t, spent_c, spent_cost, spent_lat, reserved_t, reserved_c, reserved_cost, reserved_lat = self._totals(db)
            if spent_t + reserved_t + tokens > self.budget.token_limit or (self.budget.call_limit and spent_c + reserved_c + calls > self.budget.call_limit) or (self.budget.cost_limit_micros and spent_cost + reserved_cost + cost_micros > self.budget.cost_limit_micros) or (self.budget.latency_limit_ms and spent_lat + reserved_lat + latency_ms > self.budget.latency_limit_ms):
                db.execute("ROLLBACK")
                raise BudgetExceeded("shared run budget exhausted")
            now = _now()
            db.execute("INSERT INTO budget_reservations VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (reservation_id, self.budget.run_id, work_item_id, tokens, calls, cost_micros, latency_ms, "reserved", now, expires_at))
            db.execute("COMMIT")
            return {"schema": RESERVATION_SCHEMA, "reservation_id": reservation_id, "run_id": self.budget.run_id,
                    "work_item_id": work_item_id, "tokens": tokens, "calls": calls,
                    "cost_micros": cost_micros, "latency_ms": latency_ms, "state": "reserved"}

    def settle(self, reservation_id: str, *, tokens: int, calls: int = 1, cost_micros: int = 0,
               latency_ms: int = 0, status: str = "completed") -> Dict[str, Any]:
        if any(v < 0 for v in (tokens, calls, cost_micros, latency_ms)):
            raise ValueError("usage must be non-negative")
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT payload FROM budget_settlements WHERE reservation_id=? AND run_id=?",
                (reservation_id, self.budget.run_id),
            ).fetchone()
            if prior is not None:
                return json.loads(prior[0])
            reservation = db.execute("SELECT * FROM budget_reservations WHERE reservation_id=? AND run_id=?", (reservation_id, self.budget.run_id)).fetchone()
            if reservation is None:
                db.execute("ROLLBACK")
                raise UnknownReservation(reservation_id)
            if reservation["state"] != "reserved":
                db.execute("ROLLBACK")
                raise BudgetError("reservation is not settleable")
            (spent_t, spent_c, spent_cost, spent_lat,
             reserved_t, reserved_c, reserved_cost, reserved_lat) = self._totals(db)
            # Settling replaces this reservation's estimate with actual usage; it does not
            # release capacity held by other active workers.  Account for those reservations
            # under the same BEGIN IMMEDIATE transaction so a late receipt cannot oversubscribe
            # the shared envelope while another work item is still admitted.
            other_reserved_t = reserved_t - int(reservation["estimate_tokens"])
            other_reserved_c = reserved_c - int(reservation["estimate_calls"])
            other_reserved_cost = reserved_cost - int(reservation["estimate_cost"])
            other_reserved_lat = reserved_lat - int(reservation["estimate_latency"])
            if (
                spent_t + other_reserved_t + tokens > self.budget.token_limit
                or (self.budget.call_limit
                    and spent_c + other_reserved_c + calls > self.budget.call_limit)
                or (self.budget.cost_limit_micros
                    and spent_cost + other_reserved_cost + cost_micros
                    > self.budget.cost_limit_micros)
                or (self.budget.latency_limit_ms
                    and spent_lat + other_reserved_lat + latency_ms
                    > self.budget.latency_limit_ms)
            ):
                db.execute("ROLLBACK")
                raise BudgetExceeded("late usage receipt would overspend shared run budget")
            db.execute("UPDATE budget_reservations SET state='settled' WHERE reservation_id=?", (reservation_id,))
            db.execute("UPDATE budget_runs SET spent_tokens=spent_tokens+?,spent_calls=spent_calls+?,spent_cost=spent_cost+?,spent_latency=spent_latency+? WHERE run_id=?", (tokens, calls, cost_micros, latency_ms, self.budget.run_id))
            payload = {"schema": SETTLEMENT_SCHEMA, "reservation_id": reservation_id, "run_id": self.budget.run_id,
                       "work_item_id": reservation["work_item_id"], "tokens": tokens, "calls": calls,
                       "cost_micros": cost_micros, "latency_ms": latency_ms, "status": status}
            db.execute(
                "INSERT INTO budget_settlements(reservation_id,run_id,payload,created_at) VALUES(?,?,?,?)",
                (reservation_id, self.budget.run_id, _json(payload), _now()),
            )
            db.execute("COMMIT")
            return payload

    def cancel(self, reservation_id: str) -> bool:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM budget_reservations WHERE reservation_id=? AND run_id=?", (reservation_id, self.budget.run_id)).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                raise UnknownReservation(reservation_id)
            if row["state"] == "reserved":
                db.execute("UPDATE budget_reservations SET state='cancelled' WHERE reservation_id=?", (reservation_id,))
                db.execute("COMMIT")
                return True
            db.execute("COMMIT")
            return False

    def snapshot(self) -> Dict[str, Any]:
        with self._connect() as db:
            spent_t, spent_c, spent_cost, spent_lat, reserved_t, reserved_c, reserved_cost, reserved_lat = self._totals(db)
            return {"schema": RUN_BUDGET_SCHEMA, "run_id": self.budget.run_id,
                    "limits": self.budget.as_dict(), "spent_tokens": spent_t, "spent_calls": spent_c,
                    "spent_cost_micros": spent_cost, "spent_latency_ms": spent_lat,
                    "reserved_tokens": reserved_t, "reserved_calls": reserved_c,
                    "reserved_cost_micros": reserved_cost, "reserved_latency_ms": reserved_lat,
                    "remaining_tokens": max(0, self.budget.token_limit - spent_t - reserved_t),
                    "exhaustion_policy": self.budget.exhaustion_policy}


@dataclass(frozen=True)
class ContextPackRef:
    pack_hash: str
    goal_hash: str
    relevant_fingerprint: str
    revision: int = 1

    def as_dict(self) -> Dict[str, Any]:
        return {"schema": CONTEXT_PACK_SCHEMA, "pack_hash": self.pack_hash,
                "goal_hash": self.goal_hash, "relevant_fingerprint": self.relevant_fingerprint,
                "revision": self.revision}


def context_pack_ref(*, goal: str, policy: Mapping[str, Any], acceptance: Sequence[str],
                     relevant_fingerprint: str, revision: int = 1) -> ContextPackRef:
    stable = {"goal": goal, "policy": dict(policy), "acceptance": list(acceptance)}
    pack_hash = hashlib.sha256(_json(stable).encode("utf-8")).hexdigest()
    goal_hash = hashlib.sha256(goal.encode("utf-8")).hexdigest()
    return ContextPackRef(pack_hash, goal_hash, relevant_fingerprint, revision)


def continuation_delta(events: Iterable[Mapping[str, Any]], acknowledged_cursor: int = 0,
                       *, pack: ContextPackRef, force_full: bool = False) -> Dict[str, Any]:
    """Return only sequenced events after the cursor; full history is explicit."""
    rows = [dict(event) for event in events]
    if acknowledged_cursor < 0:
        raise ValueError("acknowledged_cursor must be non-negative")
    if any(not isinstance(row.get("seq"), int) or row["seq"] < 1 for row in rows):
        raise BudgetError("continuation events require positive integer seq")
    rows.sort(key=lambda row: row["seq"])
    delta = rows if force_full else [row for row in rows if row["seq"] > acknowledged_cursor]
    return {"schema": DELTA_SCHEMA, "cursor": acknowledged_cursor,
            "next_cursor": max([acknowledged_cursor] + [row["seq"] for row in delta]),
            "pack": pack.as_dict(), "full_history": bool(force_full), "events": delta}


__all__ = ["BudgetError", "BudgetExceeded", "UnknownReservation", "RunBudget", "BudgetLedger",
           "ContextPackRef", "context_pack_ref", "continuation_delta", "RUN_BUDGET_SCHEMA",
           "RESERVATION_SCHEMA", "SETTLEMENT_SCHEMA", "CONTEXT_PACK_SCHEMA", "DELTA_SCHEMA"]
