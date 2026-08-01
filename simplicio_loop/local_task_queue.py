"""Durable local task queue composed on the existing SQLiteRemoteQueue store."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .remote_queue import Lease, QueueConflict, SQLiteRemoteQueue

SCHEMA = "simplicio.loop.local-task-queue/v1"
OUTCOMES = frozenset({
    "never_started", "running", "unknown_outcome", "verified_success",
    "retryable_failure", "blocked", "dead_letter",
})


def _digest(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class LocalTaskQueue(SQLiteRemoteQueue):
    """One crash-safe local queue; extension tables live in the inherited DB."""

    def __init__(self, root: str | Path, *, busy_timeout: float = 10.0) -> None:
        root = Path(root).resolve()
        self.orchestrator = root / ".simplicio" / "orchestrator"
        self.orchestrator.mkdir(parents=True, exist_ok=True)
        super().__init__(str(self.orchestrator / "queue.sqlite3"), busy_timeout=busy_timeout)
        self._init_local()

    def _init_local(self) -> None:
        with self._tx() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS local_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS local_dependencies(
                    task_id TEXT NOT NULL, depends_on TEXT NOT NULL,
                    PRIMARY KEY(task_id,depends_on));
                CREATE TABLE IF NOT EXISTS local_outcomes(
                    task_id TEXT PRIMARY KEY, outcome TEXT NOT NULL,
                    intent TEXT, receipt TEXT, provenance TEXT,
                    updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS local_transitions(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    from_state TEXT, to_state TEXT NOT NULL, payload TEXT NOT NULL,
                    digest TEXT NOT NULL, created_at REAL NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO local_meta VALUES('schema',?)", (SCHEMA,))
            db.execute("INSERT OR IGNORE INTO local_meta VALUES('stopped','0')")

    def _transition(self, db: sqlite3.Connection, task_id: str, old: str | None,
                    new: str, payload: Mapping[str, Any] | None = None) -> None:
        value = {"schema": SCHEMA, "task_id": task_id, "from": old,
                 "to": new, "payload": dict(payload or {}), "created_ns": time.time_ns()}
        db.execute(
            "INSERT INTO local_transitions(task_id,from_state,to_state,payload,digest,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (task_id, old, new, json.dumps(value["payload"], sort_keys=True),
             _digest(value), time.time()),
        )

    def submit(self, task_id: str, payload: Mapping[str, Any] | None = None,
               *, depends_on: Sequence[str] = ()) -> None:
        self.enqueue(task_id, {**dict(payload or {}), "depends_on": sorted(set(depends_on))})
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO local_outcomes VALUES(?,?,?,?,?,?)",
                       (task_id, "never_started", None, None, None, time.time()))
            db.executemany("INSERT OR IGNORE INTO local_dependencies VALUES(?,?)",
                           ((task_id, dep) for dep in sorted(set(depends_on))))
            self._transition(db, task_id, None, "never_started")

    def claim_local(self, task_id: str, worker_id: str, *, idempotency_key: str,
                    ttl: float = 60.0) -> Lease:
        with self._connect() as db:
            stopped = db.execute("SELECT value FROM local_meta WHERE key='stopped'").fetchone()
            deps = db.execute("SELECT depends_on FROM local_dependencies WHERE task_id=?", (task_id,)).fetchall()
            if stopped and stopped[0] == "1":
                raise QueueConflict("queue is stopped")
            for dep in deps:
                row = db.execute("SELECT outcome FROM local_outcomes WHERE task_id=?", (dep[0],)).fetchone()
                if row is None or row[0] != "verified_success":
                    raise QueueConflict("task dependencies are not verified")
        lease = self.claim(task_id, worker_id, idempotency_key=idempotency_key, ttl=ttl)
        with self._tx() as db:
            old = db.execute("SELECT outcome FROM local_outcomes WHERE task_id=?", (task_id,)).fetchone()[0]
            db.execute("UPDATE local_outcomes SET outcome='running',updated_at=? WHERE task_id=?",
                       (time.time(), task_id))
            self._transition(db, task_id, old, "running", {"fence": lease.fencing_token})
        return lease

    def persist_intent(self, lease: Lease, intent: Mapping[str, Any]) -> dict[str, Any]:
        self.assert_active(lease)
        value = {"schema": SCHEMA, "task_id": lease.task_id, "fence": lease.fencing_token,
                 "intent": dict(intent), "created_ns": time.time_ns()}
        value["digest"] = _digest(value)
        with self._tx() as db:
            db.execute("UPDATE local_outcomes SET intent=?,updated_at=? WHERE task_id=?",
                       (json.dumps(value, sort_keys=True), time.time(), lease.task_id))
        return value

    def record_outcome(self, lease: Lease, outcome: str, *, receipt: Mapping[str, Any] | None = None,
                       provenance: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if outcome not in OUTCOMES - {"never_started", "running"}:
            raise ValueError("unsafe outcome")
        self.assert_active(lease)
        if outcome == "verified_success" and receipt is None:
            raise QueueConflict("verified success requires receipt")
        if outcome == "retryable_failure" and not provenance:
            raise QueueConflict("retry requires idempotency provenance")
        value = {"schema": SCHEMA, "task_id": lease.task_id, "fence": lease.fencing_token,
                 "outcome": outcome, "receipt": dict(receipt or {}),
                 "provenance": dict(provenance or {}), "created_ns": time.time_ns()}
        value["digest"] = _digest(value)
        with self._tx() as db:
            old = db.execute("SELECT outcome FROM local_outcomes WHERE task_id=?", (lease.task_id,)).fetchone()[0]
            db.execute("UPDATE local_outcomes SET outcome=?,receipt=?,provenance=?,updated_at=? WHERE task_id=?",
                       (outcome, json.dumps(value, sort_keys=True), json.dumps(provenance or {}, sort_keys=True),
                        time.time(), lease.task_id))
            self._transition(db, lease.task_id, old, outcome, {"fence": lease.fencing_token})
            if outcome == "verified_success":
                db.execute("UPDATE tasks SET status='completed',updated_at=? WHERE task_id=?",
                           (time.time(), lease.task_id))
        return value

    def reconcile_unknown(self, task_id: str, *, verified: bool,
                          receipt: Mapping[str, Any] | None = None) -> None:
        target = "verified_success" if verified else "retryable_failure"
        if verified and receipt is None:
            raise QueueConflict("verified reconciliation requires receipt")
        with self._tx() as db:
            row = db.execute("SELECT outcome FROM local_outcomes WHERE task_id=?", (task_id,)).fetchone()
            if row is None or row[0] != "unknown_outcome":
                raise QueueConflict("task does not require reconciliation")
            db.execute("UPDATE local_outcomes SET outcome=?,receipt=?,updated_at=? WHERE task_id=?",
                       (target, json.dumps(receipt or {}, sort_keys=True), time.time(), task_id))
            db.execute("UPDATE tasks SET status=?,updated_at=? WHERE task_id=?",
                       ("completed" if verified else "ready", time.time(), task_id))
            if not verified:
                db.execute("UPDATE leases SET status='released',updated_at=? WHERE task_id=?",
                           (time.time(), task_id))
            self._transition(db, task_id, "unknown_outcome", target)

    def stop(self) -> None:
        with self._tx() as db:
            db.execute("UPDATE local_meta SET value='1' WHERE key='stopped'")
            db.execute("UPDATE leases SET cancel_requested=1 WHERE status='active'")

    def resume(self) -> None:
        with self._tx() as db:
            db.execute("UPDATE local_meta SET value='0' WHERE key='stopped'")

    def status_local(self) -> dict[str, Any]:
        with self._connect() as db:
            counts = {row[0]: row[1] for row in db.execute(
                "SELECT outcome,COUNT(*) FROM local_outcomes GROUP BY outcome")}
            stopped = db.execute("SELECT value FROM local_meta WHERE key='stopped'").fetchone()[0] == "1"
            return {"schema": SCHEMA, "stopped": stopped, "outcomes": counts,
                    "journal_mode": db.execute("PRAGMA journal_mode").fetchone()[0]}

    def inspect_local(self, task_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM local_outcomes WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            transitions = [dict(item) for item in db.execute(
                "SELECT * FROM local_transitions WHERE task_id=? ORDER BY seq", (task_id,))]
            return {"schema": SCHEMA, "task": self.task(task_id), "outcome": dict(row),
                    "transitions": transitions}

    def doctor_local(self) -> dict[str, Any]:
        try:
            with self._connect() as db:
                integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
                schema = db.execute("SELECT value FROM local_meta WHERE key='schema'").fetchone()[0]
            return {"schema": SCHEMA, "healthy": integrity == "ok" and schema == SCHEMA,
                    "integrity": integrity}
        except sqlite3.Error as exc:
            return {"schema": SCHEMA, "healthy": False, "error": str(exc)}

    def migrate(self, *, dry_run: bool = True) -> dict[str, Any]:
        backup = self.orchestrator / f"queue.sqlite3.backup-{time.time_ns()}"
        if dry_run:
            return {"schema": SCHEMA, "dry_run": True, "backup": str(backup)}
        shutil.copy2(self.path, backup)
        try:
            self._init_local()
        except Exception:
            os.replace(backup, self.path)
            raise
        return {"schema": SCHEMA, "dry_run": False, "backup": str(backup)}
