"""Durable Loop-owned lifecycle and capacity registry for agent slots.

The external coordinator may expose its own thread/worktree API, but Loop must
still own an observable, bounded capacity contract.  This registry deliberately
does not spawn processes or invoke an LLM: callers provide the spawn adapter and
receive hash-bound JSON receipts for every admission, transition, and reclaim.
"""

from __future__ import annotations

import hashlib
import argparse
import json
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional


SCHEMA = "simplicio.loop-agent-slots/v1"
RECEIPT_SCHEMA = "simplicio.loop-agent-slot-receipt/v1"
STATES = frozenset(("pending", "running", "completed", "shutdown", "reclaimable"))
ACTIVE_STATES = frozenset(("pending", "running"))
TERMINAL_STATES = frozenset(("completed", "shutdown"))


class AgentSlotError(RuntimeError):
    """Base error for invalid or conflicting slot operations."""


class AgentSlotValidationError(AgentSlotError):
    """The requested slot record is invalid."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _agent_id(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 256:
        raise AgentSlotValidationError("agent_id must be non-empty bounded text")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise AgentSlotValidationError("agent_id contains control characters")
    return value


def _non_negative_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AgentSlotValidationError("%s must be a non-negative integer" % name)
    return value


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise AgentSlotValidationError("%s must be boolean" % name)
    return value


class AgentSlotRegistry:
    """SQLite-backed capacity authority with idempotent terminal reclaim."""

    def __init__(self, path: Path, *, capacity: int = 6, retry_limit: int = 1) -> None:
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
            raise AgentSlotValidationError("capacity must be a positive integer")
        self.path = Path(path)
        self.capacity = capacity
        self.retry_limit = _non_negative_int(retry_limit, "retry_limit")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS agent_slot_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = db.execute("SELECT value FROM agent_slot_meta WHERE key='schema'").fetchone()
            if row is None:
                db.execute("INSERT INTO agent_slot_meta(key,value) VALUES ('schema',?)", (SCHEMA,))
                db.execute("INSERT INTO agent_slot_meta(key,value) VALUES ('capacity',?)", (str(self.capacity),))
            elif row["value"] != SCHEMA:
                raise AgentSlotValidationError("unsupported agent slot schema")
            stored = db.execute("SELECT value FROM agent_slot_meta WHERE key='capacity'").fetchone()
            if stored is None:
                db.execute("INSERT INTO agent_slot_meta(key,value) VALUES ('capacity',?)", (str(self.capacity),))
            elif int(stored["value"]) != self.capacity:
                raise AgentSlotValidationError("capacity conflicts with the persisted registry")
            db.execute(
                """CREATE TABLE IF NOT EXISTS agent_slots (
                    agent_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    worktree TEXT,
                    lease_id TEXT,
                    descendants INTEGER NOT NULL,
                    worktree_active INTEGER NOT NULL,
                    lease_active INTEGER NOT NULL,
                    reason TEXT,
                    created_ns INTEGER NOT NULL,
                    updated_ns INTEGER NOT NULL
                )"""
            )

    @staticmethod
    def _record(row: sqlite3.Row) -> Dict[str, Any]:
        record = dict(row)
        record["descendants"] = int(record["descendants"])
        record["worktree_active"] = bool(record["worktree_active"])
        record["lease_active"] = bool(record["lease_active"])
        record["reclaimable"] = (
            record["status"] in TERMINAL_STATES | frozenset(("reclaimable",))
            and not AgentSlotRegistry._blockers(record)
        )
        return record

    @staticmethod
    def _blockers(record: Mapping[str, Any]) -> List[str]:
        blockers: List[str] = []
        if int(record.get("descendants", 0)) > 0:
            blockers.append("descendants")
        if bool(record.get("worktree_active", False)):
            blockers.append("worktree")
        if bool(record.get("lease_active", False)):
            blockers.append("lease")
        return blockers

    def _receipt(
        self,
        operation: str,
        *,
        accepted: bool,
        reason_code: str,
        agent_id: str = "",
        status: Optional[str] = None,
        attempt: Optional[int] = None,
        diagnostics: Optional[Mapping[str, Any]] = None,
        active_slots: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "schema": RECEIPT_SCHEMA,
            "contract": SCHEMA,
            "operation": operation,
            "accepted": accepted,
            "reason_code": reason_code,
            "agent_id": agent_id,
            "status": status,
            "attempt": attempt,
            "diagnostics": dict(diagnostics or {}),
            "active_slots": active_slots,
            "capacity": self.capacity,
            "local_llm": False,
        }
        payload["receipt_hash"] = _digest(payload)
        return payload

    def _snapshot(self, db: sqlite3.Connection) -> Dict[str, Any]:
        rows = [self._record(row) for row in db.execute("SELECT * FROM agent_slots ORDER BY agent_id")]
        counts = {state: 0 for state in sorted(STATES)}
        for record in rows:
            counts[record["status"]] += 1
        # Keep completed/shutdown history visible while exposing reclaimability
        # as a derived status capability instead of deleting audit records.
        counts["reclaimable"] = sum(1 for record in rows if record["reclaimable"])
        active = sum(counts[state] for state in ACTIVE_STATES)
        diagnostics = []
        for record in rows:
            blockers = self._blockers(record)
            if blockers:
                diagnostics.append({"agent_id": record["agent_id"], "status": record["status"], "blockers": blockers})
        return {
            "schema": SCHEMA,
            "capacity": self.capacity,
            "active_slots": active,
            "available_slots": self.capacity - active,
            "counts": counts,
            "records": rows,
            "diagnostics": diagnostics,
            "capacity_holders": [record["agent_id"] for record in rows if record["status"] in ACTIVE_STATES],
            "local_llm": False,
        }

    def status(self) -> Dict[str, Any]:
        db = self._connect()
        try:
            return self._snapshot(db)
        finally:
            db.close()

    def acquire(
        self,
        agent_id: str,
        *,
        worktree: Optional[str] = None,
        lease_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        agent_id = _agent_id(agent_id)
        now = time.time_ns()
        with self._transaction() as db:
            existing = db.execute("SELECT * FROM agent_slots WHERE agent_id=?", (agent_id,)).fetchone()
            if existing is not None:
                record = self._record(existing)
                snapshot = self._snapshot(db)
                return self._receipt(
                    "acquire", accepted=False, reason_code="duplicate_agent",
                    agent_id=agent_id, status=record["status"], attempt=record["attempt"],
                    diagnostics={"existing": record, "snapshot": snapshot},
                    active_slots=snapshot["active_slots"],
                )
            snapshot = self._snapshot(db)
            if snapshot["active_slots"] >= self.capacity:
                return self._receipt(
                    "acquire", accepted=False, reason_code="slot_capacity_exhausted",
                    agent_id=agent_id, diagnostics={
                        "active_slots": snapshot["active_slots"],
                        "available_slots": snapshot["available_slots"],
                        "capacity_holders": snapshot["capacity_holders"],
                        "blockers": snapshot["diagnostics"],
                    }, active_slots=snapshot["active_slots"],
                )
            db.execute(
                """INSERT INTO agent_slots(
                    agent_id,status,attempt,worktree,lease_id,descendants,worktree_active,lease_active,
                    reason,created_ns,updated_ns
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (agent_id, "pending", 1, worktree, lease_id, 0, 0, 0, None, now, now),
            )
            return self._receipt(
                "acquire", accepted=True, reason_code="slot_acquired", agent_id=agent_id,
                status="pending", attempt=1, active_slots=snapshot["active_slots"] + 1,
            )

    def start(self, agent_id: str) -> Dict[str, Any]:
        return self._transition(agent_id, "running", "slot_started")

    def close_agent(self, agent_id: str, *, status: str = "completed", reason: str = "") -> Dict[str, Any]:
        if status not in TERMINAL_STATES:
            raise AgentSlotValidationError("close status must be completed or shutdown")
        return self._transition(agent_id, status, reason or "agent_terminal")

    def _transition(self, agent_id: str, target: str, reason: str) -> Dict[str, Any]:
        agent_id = _agent_id(agent_id)
        with self._transaction() as db:
            row = db.execute("SELECT * FROM agent_slots WHERE agent_id=?", (agent_id,)).fetchone()
            if row is None:
                return self._receipt("transition", accepted=False, reason_code="unknown_agent", agent_id=agent_id)
            record = self._record(row)
            current = record["status"]
            if current == target:
                snapshot = self._snapshot(db)
                return self._receipt(
                    "transition", accepted=False, reason_code="idempotent", agent_id=agent_id,
                    status=current, attempt=record["attempt"], active_slots=snapshot["active_slots"],
                )
            allowed = (current == "pending" and target == "running") or (
                current in ACTIVE_STATES and target in TERMINAL_STATES
            )
            if not allowed:
                return self._receipt(
                    "transition", accepted=False, reason_code="invalid_transition", agent_id=agent_id,
                    status=current, attempt=record["attempt"], diagnostics={"target": target},
                    active_slots=self._snapshot(db)["active_slots"],
                )
            db.execute(
                "UPDATE agent_slots SET status=?,reason=?,updated_ns=? WHERE agent_id=?",
                (target, reason, time.time_ns(), agent_id),
            )
            snapshot = self._snapshot(db)
            return self._receipt(
                "transition", accepted=True, reason_code=reason, agent_id=agent_id,
                status=target, attempt=record["attempt"], active_slots=snapshot["active_slots"],
                diagnostics={"capacity_released": target in TERMINAL_STATES},
            )

    def update_blockers(
        self,
        agent_id: str,
        *,
        descendants: int = 0,
        worktree_active: bool = False,
        lease_active: bool = False,
    ) -> Dict[str, Any]:
        agent_id = _agent_id(agent_id)
        descendants = _non_negative_int(descendants, "descendants")
        worktree_active = _bool(worktree_active, "worktree_active")
        lease_active = _bool(lease_active, "lease_active")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM agent_slots WHERE agent_id=?", (agent_id,)).fetchone()
            if row is None:
                return self._receipt("update_blockers", accepted=False, reason_code="unknown_agent", agent_id=agent_id)
            db.execute(
                "UPDATE agent_slots SET descendants=?,worktree_active=?,lease_active=?,updated_ns=? WHERE agent_id=?",
                (descendants, int(worktree_active), int(lease_active), time.time_ns(), agent_id),
            )
            snapshot = self._snapshot(db)
            return self._receipt(
                "update_blockers", accepted=True, reason_code="blockers_updated", agent_id=agent_id,
                status=row["status"], attempt=row["attempt"],
                diagnostics={"blockers": self._blockers({"descendants": descendants,
                                                           "worktree_active": worktree_active,
                                                           "lease_active": lease_active})},
                active_slots=snapshot["active_slots"],
            )

    def reclaim(self, agent_id: Optional[str] = None) -> Dict[str, Any]:
        selected = _agent_id(agent_id) if agent_id is not None else None
        with self._transaction() as db:
            query = "SELECT * FROM agent_slots WHERE status IN ('completed','shutdown','reclaimable')"
            params: tuple[Any, ...] = ()
            if selected is not None:
                query += " AND agent_id=?"
                params = (selected,)
            rows = [self._record(row) for row in db.execute(query, params)]
            reclaimed: List[str] = []
            blocked: List[Dict[str, Any]] = []
            for record in rows:
                blockers = self._blockers(record)
                if blockers:
                    blocked.append({"agent_id": record["agent_id"], "status": record["status"], "blockers": blockers})
                    continue
                reclaimed.append(record["agent_id"])
            snapshot = self._snapshot(db)
            return self._receipt(
                "reclaim", accepted=not blocked, reason_code="slots_reclaimed" if not blocked else "reclaim_blocked",
                agent_id=selected or "", diagnostics={"reclaimed": reclaimed, "blocked": blocked,
                                                          "available_slots": snapshot["available_slots"]},
                active_slots=snapshot["active_slots"],
            )

    def _retry_reopen(self, agent_id: str) -> Dict[str, Any]:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM agent_slots WHERE agent_id=?", (agent_id,)).fetchone()
            if row is None or row["status"] not in ("completed", "shutdown", "reclaimable"):
                return self._receipt("retry", accepted=False, reason_code="retry_not_available", agent_id=agent_id)
            record = self._record(row)
            if self._blockers(record):
                return self._receipt("retry", accepted=False, reason_code="retry_blocked", agent_id=agent_id,
                                     status=record["status"], attempt=record["attempt"],
                                     diagnostics={"blockers": self._blockers(record)})
            snapshot = self._snapshot(db)
            if snapshot["active_slots"] >= self.capacity:
                return self._receipt("retry", accepted=False, reason_code="slot_capacity_exhausted", agent_id=agent_id,
                                     diagnostics={"snapshot": snapshot}, active_slots=snapshot["active_slots"])
            attempt = int(record["attempt"]) + 1
            db.execute("UPDATE agent_slots SET status='pending',attempt=?,reason=?,updated_ns=? WHERE agent_id=?",
                       (attempt, "bounded_retry", time.time_ns(), agent_id))
            return self._receipt("retry", accepted=True, reason_code="bounded_retry", agent_id=agent_id,
                                 status="pending", attempt=attempt, active_slots=snapshot["active_slots"] + 1)

    def spawn_batch(
        self,
        agent_ids: Iterable[str],
        spawn: Callable[[str, Mapping[str, Any]], Any],
        *,
        retry_limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        limit = self.retry_limit if retry_limit is None else _non_negative_int(retry_limit, "retry_limit")
        results: List[Dict[str, Any]] = []
        for raw_agent_id in agent_ids:
            agent_id = _agent_id(raw_agent_id)
            attempts = 0
            item: Dict[str, Any] = {"agent_id": agent_id, "attempts": 0, "success": False, "receipts": []}
            while True:
                acquired = self.acquire(agent_id)
                if not acquired["accepted"]:
                    if acquired["reason_code"] == "duplicate_agent" and attempts > 0:
                        reopened = self._retry_reopen(agent_id)
                        item["receipts"].append(reopened)
                        if not reopened["accepted"]:
                            item["reason_code"] = reopened["reason_code"]
                            break
                    else:
                        item["reason_code"] = acquired["reason_code"]
                        item["receipts"].append(acquired)
                        break
                else:
                    item["receipts"].append(acquired)
                current = self.status()["records"]
                record = next(record for record in current if record["agent_id"] == agent_id)
                item["attempts"] += 1
                try:
                    outcome = spawn(agent_id, record)
                    success = bool(outcome.get("success", False)) if isinstance(outcome, Mapping) else bool(outcome)
                    error = "" if success else "spawn_returned_failure"
                except Exception as exc:  # adapters are isolated to one bounded lane
                    success = False
                    error = str(exc)[:512] or exc.__class__.__name__
                if success:
                    started = self.start(agent_id)
                    item["receipts"].append(started)
                    item["success"] = started["accepted"] or started["reason_code"] == "idempotent"
                    item["reason_code"] = "spawned" if item["success"] else started["reason_code"]
                    break
                closed = self.close_agent(agent_id, status="shutdown", reason="spawn_failed")
                item["receipts"].append(closed)
                reclaimed = self.reclaim(agent_id)
                item["receipts"].append(reclaimed)
                item["error"] = error
                if attempts >= limit or not reclaimed["accepted"]:
                    item["reason_code"] = "spawn_failed_retry_exhausted" if attempts >= limit else reclaimed["reason_code"]
                    break
                attempts += 1
            results.append(item)
        return {"schema": SCHEMA, "operation": "spawn_batch", "retry_limit": limit,
                "results": results, "status": self.status(), "local_llm": False}


__all__ = ["AgentSlotError", "AgentSlotRegistry", "AgentSlotValidationError", "SCHEMA", "RECEIPT_SCHEMA"]


def cli_main(argv: Optional[Iterable[str]] = None) -> int:
    """Run the JSON-first, read-only-by-default slot lifecycle CLI."""
    parser = argparse.ArgumentParser(prog="simplicio-loop agent-slots")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--db", default=".simplicio/orchestrator/agent-slots.sqlite")
        command_parser.add_argument("--route", choices=("legacy", "mapper"), default="legacy")
        command_parser.add_argument("--mapper-db", default=None)
        command_parser.add_argument("--mapper-init", action="store_true")
        command_parser.add_argument("--capacity", type=int, default=6)
        command_parser.add_argument("--retry-limit", type=int, default=1)

    status_parser = sub.add_parser("status")
    common(status_parser)
    acquire_parser = sub.add_parser("acquire")
    acquire_parser.add_argument("agent_id")
    acquire_parser.add_argument("--worktree", default=None)
    acquire_parser.add_argument("--lease-id", default=None)
    common(acquire_parser)
    start_parser = sub.add_parser("start")
    start_parser.add_argument("agent_id")
    common(start_parser)
    close_parser = sub.add_parser("close")
    close_parser.add_argument("agent_id")
    close_parser.add_argument("--status", choices=tuple(TERMINAL_STATES), default="completed")
    close_parser.add_argument("--reason", default="")
    common(close_parser)
    reclaim_parser = sub.add_parser("reclaim")
    reclaim_parser.add_argument("agent_id", nargs="?")
    common(reclaim_parser)
    blockers_parser = sub.add_parser("update-blockers")
    blockers_parser.add_argument("agent_id")
    blockers_parser.add_argument("--descendants", type=int, default=0)
    blockers_parser.add_argument("--worktree-active", action="store_true")
    blockers_parser.add_argument("--lease-active", action="store_true")
    common(blockers_parser)

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.route == "mapper":
        if not args.mapper_db:
            parser.error("--mapper-db is required with --route mapper")
        from .mapper_agent_slots import MapperAgentSlotRegistry
        registry = MapperAgentSlotRegistry(args.mapper_db, capacity=args.capacity, auto_create=False)
        if args.mapper_init:
            registry.initialize()
    else:
        registry = AgentSlotRegistry(Path(args.db), capacity=args.capacity, retry_limit=args.retry_limit)
    if args.command == "status":
        result = registry.status()
    elif args.command == "acquire":
        result = registry.acquire(args.agent_id, worktree=args.worktree, lease_id=args.lease_id)
    elif args.command == "start":
        result = registry.start(args.agent_id)
    elif args.command == "close":
        result = registry.close_agent(args.agent_id, status=args.status, reason=args.reason)
    elif args.command == "reclaim":
        result = registry.reclaim(args.agent_id)
    else:
        result = registry.update_blockers(
            args.agent_id,
            descendants=args.descendants,
            worktree_active=args.worktree_active,
            lease_active=args.lease_active,
        )
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    return 0
