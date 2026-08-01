"""Durable local task queue composed on the existing SQLiteRemoteQueue store."""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .remote_queue import (
    Lease, QueueConflict, QueueUnavailable, SQLiteRemoteQueue, _lease_id, _now,
)

SCHEMA = "simplicio.loop.local-task-queue/v2"
LEGACY_SCHEMA = "simplicio.loop.local-task-queue/v1"
OUTCOMES = frozenset({
    "never_started", "running", "unknown_outcome", "verified_success",
    "retryable_failure", "blocked", "dead_letter",
})


def _digest(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class LocalTaskQueue(SQLiteRemoteQueue):
    """One crash-safe local queue; extension tables live in the inherited DB."""

    def __init__(self, root: str | Path, *, busy_timeout: float = 10.0,
                 allow_legacy: bool = False) -> None:
        root = Path(root).resolve()
        if str(root).startswith("\\\\"):
            raise QueueUnavailable("network filesystem locking is not trusted")
        self.orchestrator = root / ".simplicio" / "orchestrator"
        self.orchestrator.mkdir(parents=True, exist_ok=True)
        super().__init__(str(self.orchestrator / "queue.sqlite3"), busy_timeout=busy_timeout)
        self._init_local(allow_legacy=allow_legacy)

    def _init_local(self, *, allow_legacy: bool = False) -> None:
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
            stored = db.execute("SELECT value FROM local_meta WHERE key='schema'").fetchone()[0]
            if stored != SCHEMA and not (allow_legacy and stored == LEGACY_SCHEMA):
                raise QueueUnavailable(
                    f"unsupported local queue schema {stored!r}; run `simplicio-loop queue migrate`"
                )

    def _transition(self, db: sqlite3.Connection, task_id: str, old: str | None,
                    new: str, payload: Mapping[str, Any] | None = None) -> None:
        value = {"schema": SCHEMA, "task_id": task_id, "from": old,
                 "to": new, "payload": dict(payload or {}), "created_ns": time.time_ns()}
        db.execute(
            "INSERT INTO local_transitions(task_id,from_state,to_state,payload,digest,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (task_id, old, new, json.dumps(value, sort_keys=True),
             _digest(value), time.time()),
        )

    def submit(self, task_id: str, payload: Mapping[str, Any] | None = None,
               *, depends_on: Sequence[str] = ()) -> None:
        task_id = str(task_id).strip()
        if not task_id:
            raise ValueError("task_id is required")
        dependencies = sorted(set(map(str, depends_on)))
        task_payload = {**dict(payload or {}), "depends_on": dependencies}
        with self._tx() as db:
            stopped = db.execute("SELECT value FROM local_meta WHERE key='stopped'").fetchone()
            if stopped and stopped[0] == "1":
                raise QueueConflict("queue is stopped")
            db.execute("INSERT OR IGNORE INTO tasks(task_id,status,payload,updated_at) VALUES(?,?,?,?)",
                       (task_id, "ready", json.dumps(task_payload, sort_keys=True), time.time()))
            if db.execute("SELECT 1 FROM local_outcomes WHERE task_id=?", (task_id,)).fetchone():
                return
            self._event(db, task_id, "enqueued", "system", None, task_payload)
            db.execute("INSERT INTO local_outcomes VALUES(?,?,?,?,?,?)",
                       (task_id, "never_started", None, None, None, time.time()))
            db.executemany("INSERT OR IGNORE INTO local_dependencies VALUES(?,?)",
                           ((task_id, dep) for dep in dependencies))
            self._transition(db, task_id, None, "never_started")

    def claim_local(self, task_id: str, worker_id: str, *, idempotency_key: str,
                    ttl: float = 60.0) -> Lease:
        if ttl <= 0 or not worker_id or not idempotency_key:
            raise ValueError("worker_id, idempotency_key and positive ttl are required")
        with self._tx() as db:
            stopped = db.execute("SELECT value FROM local_meta WHERE key='stopped'").fetchone()
            deps = db.execute("SELECT depends_on FROM local_dependencies WHERE task_id=?", (task_id,)).fetchall()
            if stopped and stopped[0] == "1":
                raise QueueConflict("queue is stopped")
            for dep in deps:
                row = db.execute("SELECT outcome FROM local_outcomes WHERE task_id=?", (dep[0],)).fetchone()
                if row is None or row[0] != "verified_success":
                    raise QueueConflict("task dependencies are not verified")
            now = _now()
            lease = self._claim_in_tx(
                db, task_id, worker_id, idempotency_key, ttl, None, (), now,
                _lease_id(task_id, worker_id, idempotency_key),
            )
            old = db.execute("SELECT outcome FROM local_outcomes WHERE task_id=?", (task_id,)).fetchone()[0]
            db.execute("UPDATE local_outcomes SET outcome='running',updated_at=? WHERE task_id=?",
                       (time.time(), task_id))
            self._transition(db, task_id, old, "running", {"fence": lease.fencing_token})
        return lease

    def persist_intent(self, lease: Lease, intent: Mapping[str, Any]) -> dict[str, Any]:
        value = {"schema": SCHEMA, "task_id": lease.task_id, "fence": lease.fencing_token,
                 "intent": dict(intent), "created_ns": time.time_ns()}
        value["digest"] = _digest(value)
        with self._tx() as db:
            self._owned(db, lease)
            db.execute("UPDATE local_outcomes SET intent=?,updated_at=? WHERE task_id=?",
                       (json.dumps(value, sort_keys=True), time.time(), lease.task_id))
        return value

    def record_outcome(self, lease: Lease, outcome: str, *, receipt: Mapping[str, Any] | None = None,
                       provenance: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if outcome not in OUTCOMES - {"never_started", "running"}:
            raise ValueError("unsafe outcome")
        if outcome == "verified_success" and receipt is None:
            raise QueueConflict("verified success requires receipt")
        if outcome == "retryable_failure" and not provenance:
            raise QueueConflict("retry requires idempotency provenance")
        value = {"schema": SCHEMA, "task_id": lease.task_id, "fence": lease.fencing_token,
                 "outcome": outcome, "receipt": dict(receipt or {}),
                 "provenance": dict(provenance or {}), "created_ns": time.time_ns()}
        value["digest"] = _digest(value)
        provenance_value = None
        if provenance:
            provenance_value = {"schema": SCHEMA, "task_id": lease.task_id,
                                "provenance": dict(provenance), "created_ns": time.time_ns()}
            provenance_value["digest"] = _digest(provenance_value)
        with self._tx() as db:
            self._owned(db, lease)
            old = db.execute("SELECT outcome FROM local_outcomes WHERE task_id=?", (lease.task_id,)).fetchone()[0]
            if old in {"verified_success", "blocked", "dead_letter"}:
                raise QueueConflict("terminal outcome is immutable")
            db.execute("UPDATE local_outcomes SET outcome=?,receipt=?,provenance=?,updated_at=? WHERE task_id=?",
                       (outcome, json.dumps(value, sort_keys=True),
                        json.dumps(provenance_value, sort_keys=True) if provenance_value else None,
                        time.time(), lease.task_id))
            self._transition(db, lease.task_id, old, outcome, {"fence": lease.fencing_token})
            task_status = {
                "verified_success": "completed", "dead_letter": "completed",
                "blocked": "cancelled", "retryable_failure": "ready",
            }.get(outcome, "claimed")
            db.execute("UPDATE tasks SET status=?,updated_at=? WHERE task_id=?",
                       (task_status, time.time(), lease.task_id))
            if outcome in {"verified_success", "blocked", "dead_letter", "retryable_failure"}:
                lease_status = "completed" if outcome in {"verified_success", "dead_letter"} else "released"
                db.execute("UPDATE leases SET status=?,updated_at=? WHERE task_id=?",
                           (lease_status, time.time(), lease.task_id))
        return value

    def reconcile_unknown(self, task_id: str, *, verified: bool,
                          receipt: Mapping[str, Any] | None = None,
                          provenance: Mapping[str, Any] | None = None) -> None:
        target = "verified_success" if verified else "retryable_failure"
        if verified and receipt is None:
            raise QueueConflict("verified reconciliation requires receipt")
        if not verified and not provenance:
            raise QueueConflict("retry reconciliation requires idempotency provenance")
        receipt_value = None
        if receipt is not None:
            receipt_value = {"schema": SCHEMA, "task_id": task_id, "outcome": target,
                             "receipt": dict(receipt), "created_ns": time.time_ns()}
            receipt_value["digest"] = _digest(receipt_value)
        provenance_value = None
        if provenance:
            provenance_value = {"schema": SCHEMA, "task_id": task_id,
                                "provenance": dict(provenance), "created_ns": time.time_ns()}
            provenance_value["digest"] = _digest(provenance_value)
        with self._tx() as db:
            row = db.execute("SELECT outcome FROM local_outcomes WHERE task_id=?", (task_id,)).fetchone()
            if row is None or row[0] != "unknown_outcome":
                raise QueueConflict("task does not require reconciliation")
            db.execute("UPDATE local_outcomes SET outcome=?,receipt=?,provenance=?,updated_at=? WHERE task_id=?",
                       (target, json.dumps(receipt_value, sort_keys=True) if receipt_value else None,
                        json.dumps(provenance_value, sort_keys=True) if provenance_value else None,
                        time.time(), task_id))
            db.execute("UPDATE tasks SET status=?,updated_at=? WHERE task_id=?",
                       ("completed" if verified else "ready", time.time(), task_id))
            db.execute("UPDATE leases SET status=?,updated_at=? WHERE task_id=?",
                       ("completed" if verified else "released", time.time(), task_id))
            self._transition(db, task_id, "unknown_outcome", target)

    def stop(self) -> None:
        with self._tx() as db:
            db.execute("UPDATE local_meta SET value='1' WHERE key='stopped'")
            db.execute("UPDATE leases SET cancel_requested=1 WHERE status='active'")

    def cancel_local(self, task_id: str, *, reason: str = "operator_cancelled") -> dict[str, Any]:
        with self._tx() as db:
            task = db.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(task_id)
            old = db.execute("SELECT outcome FROM local_outcomes WHERE task_id=?", (task_id,)).fetchone()[0]
            if old in {"verified_success", "blocked", "dead_letter"}:
                raise QueueConflict("terminal outcome is immutable")
            lease = db.execute("SELECT status,expires_at FROM leases WHERE task_id=?", (task_id,)).fetchone()
            if lease and lease["status"] == "active" and lease["expires_at"] > time.time():
                db.execute("UPDATE leases SET cancel_requested=1,updated_at=? WHERE task_id=?",
                           (time.time(), task_id))
                return {"schema": SCHEMA, "task_id": task_id, "status": "cancelling"}
            db.execute("UPDATE local_outcomes SET outcome='blocked',updated_at=? WHERE task_id=?",
                       (time.time(), task_id))
            db.execute("UPDATE tasks SET status='cancelled',updated_at=? WHERE task_id=?",
                       (time.time(), task_id))
            self._transition(db, task_id, old, "blocked", {"reason": reason})
            return {"schema": SCHEMA, "task_id": task_id, "status": "cancelled"}

    def reclaim_stale(self, *, now: float | None = None) -> list[str]:
        current = time.time() if now is None else float(now)
        reclaimed: list[str] = []
        with self._tx() as db:
            rows = db.execute(
                "SELECT task_id FROM leases WHERE status='active' AND expires_at<=? ORDER BY task_id",
                (current,),
            ).fetchall()
            for row in rows:
                task_id = row[0]
                old = db.execute("SELECT outcome FROM local_outcomes WHERE task_id=?", (task_id,)).fetchone()[0]
                db.execute("UPDATE leases SET status='expired',updated_at=? WHERE task_id=?",
                           (current, task_id))
                db.execute("UPDATE local_outcomes SET outcome='unknown_outcome',updated_at=? WHERE task_id=?",
                           (current, task_id))
                self._transition(db, task_id, old, "unknown_outcome", {"reason": "lease_expired"})
                reclaimed.append(task_id)
        return reclaimed

    def drain(self, *, timeout: float = 0.0) -> dict[str, Any]:
        self.stop()
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with contextlib.closing(self._connect()) as db:
                active = db.execute(
                    "SELECT COUNT(*) FROM leases WHERE status='active' AND expires_at>?", (time.time(),)
                ).fetchone()[0]
            if active == 0 or time.monotonic() >= deadline:
                return {"schema": SCHEMA, "status": "drained" if active == 0 else "cancelling",
                        "active": active}
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))

    def resume(self) -> None:
        with self._tx() as db:
            db.execute("UPDATE local_meta SET value='0' WHERE key='stopped'")

    def status_local(self) -> dict[str, Any]:
        with contextlib.closing(self._connect()) as db:
            counts = {row[0]: row[1] for row in db.execute(
                "SELECT outcome,COUNT(*) FROM local_outcomes GROUP BY outcome")}
            stopped = db.execute("SELECT value FROM local_meta WHERE key='stopped'").fetchone()[0] == "1"
            return {"schema": SCHEMA, "stopped": stopped, "outcomes": counts,
                    "journal_mode": db.execute("PRAGMA journal_mode").fetchone()[0]}

    def top(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return self.pull("operator", limit=limit)

    def inspect_local(self, task_id: str) -> dict[str, Any]:
        with contextlib.closing(self._connect()) as db:
            row = db.execute("SELECT * FROM local_outcomes WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            transitions = [dict(item) for item in db.execute(
                "SELECT * FROM local_transitions WHERE task_id=? ORDER BY seq", (task_id,))]
            return {"schema": SCHEMA, "task": self.task(task_id), "outcome": dict(row),
                    "transitions": transitions}

    def doctor_local(self) -> dict[str, Any]:
        try:
            with contextlib.closing(self._connect()) as db:
                integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
                schema = db.execute("SELECT value FROM local_meta WHERE key='schema'").fetchone()[0]
                missing = [row[0] for row in db.execute(
                    "SELECT t.task_id FROM tasks t LEFT JOIN local_outcomes o USING(task_id) "
                    "WHERE o.task_id IS NULL ORDER BY t.task_id")]
                corrupt = []
                for row in db.execute("SELECT seq,task_id,from_state,to_state,payload,digest FROM local_transitions"):
                    try:
                        value = json.loads(row["payload"])
                        if value.get("schema") != SCHEMA or value.get("task_id") != row["task_id"] \
                                or value.get("from") != row["from_state"] or value.get("to") != row["to_state"] \
                                or _digest(value) != row["digest"]:
                            corrupt.append(row["seq"])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        corrupt.append(row["seq"])
                corrupt_records = []
                for row in db.execute("SELECT task_id,intent,receipt,provenance FROM local_outcomes"):
                    for field in ("intent", "receipt", "provenance"):
                        raw = row[field]
                        if not raw:
                            continue
                        try:
                            value = json.loads(raw)
                            supplied = value.pop("digest", "")
                            if value.get("schema") != SCHEMA or value.get("task_id") != row["task_id"] \
                                    or supplied != _digest(value):
                                corrupt_records.append(f"{row['task_id']}:{field}")
                        except (TypeError, ValueError, json.JSONDecodeError):
                            corrupt_records.append(f"{row['task_id']}:{field}")
            return {"schema": SCHEMA,
                    "healthy": integrity == "ok" and schema == SCHEMA and not missing and not corrupt and not corrupt_records,
                    "integrity": integrity, "missing_outcomes": missing,
                    "corrupt_transitions": corrupt, "corrupt_records": corrupt_records}
        except sqlite3.Error as exc:
            return {"schema": SCHEMA, "healthy": False, "error": str(exc)}

    def migrate(self, *, dry_run: bool = True) -> dict[str, Any]:
        backup = self.orchestrator / f"queue.sqlite3.backup-{time.time_ns()}"
        if dry_run:
            return {"schema": SCHEMA, "dry_run": True, "backup": str(backup)}
        with contextlib.closing(self._connect()) as source, \
                contextlib.closing(sqlite3.connect(backup)) as destination:
            source.backup(destination)
        try:
            with self._tx() as db:
                stored = db.execute("SELECT value FROM local_meta WHERE key='schema'").fetchone()[0]
                if stored not in {SCHEMA, LEGACY_SCHEMA}:
                    raise QueueUnavailable(f"unsupported local queue schema {stored!r}")
                migrated = 0
                migrated_provenance = 0
                if stored == LEGACY_SCHEMA:
                    rows = db.execute("SELECT task_id,intent,receipt,provenance FROM local_outcomes").fetchall()
                    transitions = db.execute("SELECT seq,payload,digest FROM local_transitions").fetchall()
                    # Authenticate every versioned v1 envelope before the first
                    # migration write. Re-hashing untrusted bytes would turn a
                    # forged legacy receipt into apparently healthy v2 evidence.
                    for row in rows:
                        for field in ("intent", "receipt", "provenance"):
                            if not row[field]:
                                continue
                            try:
                                raw = json.loads(row[field])
                            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                                raise QueueUnavailable(f"invalid legacy {field} for {row['task_id']}") from exc
                            if not isinstance(raw, dict):
                                raise QueueUnavailable(f"invalid legacy {field} for {row['task_id']}")
                            if "schema" in raw or "digest" in raw:
                                supplied = raw.pop("digest", "")
                                if raw.get("schema") not in {LEGACY_SCHEMA, SCHEMA} or supplied != _digest(raw):
                                    raise QueueUnavailable(f"invalid legacy {field} digest for {row['task_id']}")
                    for row in transitions:
                        try:
                            payload = json.loads(row["payload"])
                        except (TypeError, ValueError, json.JSONDecodeError) as exc:
                            raise QueueUnavailable(f"invalid legacy transition {row['seq']}") from exc
                        if (not isinstance(payload, dict) or payload.get("schema") not in {LEGACY_SCHEMA, SCHEMA}
                                or row["digest"] != _digest(payload)):
                            raise QueueUnavailable(f"invalid legacy transition digest {row['seq']}")
                    for row in rows:
                        for field in ("intent", "receipt", "provenance"):
                            if not row[field]:
                                continue
                            try:
                                raw = json.loads(row[field])
                            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                                raise QueueUnavailable(f"invalid legacy {field} for {row['task_id']}") from exc
                            if not isinstance(raw, dict):
                                raise QueueUnavailable(f"invalid legacy {field} for {row['task_id']}")
                            if field == "provenance" and "schema" not in raw:
                                raw = {"schema": SCHEMA, "task_id": row["task_id"],
                                       "provenance": raw, "created_ns": time.time_ns()}
                            else:
                                raw["schema"] = SCHEMA
                                raw["task_id"] = row["task_id"]
                                raw.pop("digest", None)
                            raw["digest"] = _digest(raw)
                            db.execute(f"UPDATE local_outcomes SET {field}=? WHERE task_id=?",
                                       (json.dumps(raw, sort_keys=True), row["task_id"]))
                            migrated += 1
                            if field == "provenance":
                                migrated_provenance += 1
                    for row in transitions:
                        try:
                            payload = json.loads(row["payload"])
                        except (TypeError, ValueError, json.JSONDecodeError) as exc:
                            raise QueueUnavailable(f"invalid legacy transition {row['seq']}") from exc
                        if not isinstance(payload, dict):
                            raise QueueUnavailable(f"invalid legacy transition {row['seq']}")
                        payload["schema"] = SCHEMA
                        db.execute("UPDATE local_transitions SET payload=?,digest=? WHERE seq=?",
                                   (json.dumps(payload, sort_keys=True), _digest(payload), row["seq"]))
                        migrated += 1
                    db.execute("UPDATE local_meta SET value=? WHERE key='schema'", (SCHEMA,))
            self._init_local()
            validation = self.doctor_local()
            if not validation.get("healthy"):
                raise QueueUnavailable(f"post-migration validation failed: {validation}")
        except Exception:
            # Restore through SQLite rather than replacing the live database file;
            # Windows can retain a WAL/shared-memory handle between connections.
            with contextlib.closing(sqlite3.connect(backup)) as source, \
                    contextlib.closing(sqlite3.connect(self.path)) as destination:
                source.backup(destination)
            raise
        return {"schema": SCHEMA, "dry_run": False, "backup": str(backup),
                "from_schema": stored, "migrated_records": migrated,
                "migrated_provenance": migrated_provenance}

    def gc_terminal(self, *, apply: bool = False) -> dict[str, Any]:
        eligible: list[str] = []
        with self._tx() as db:
            rows = db.execute(
                "SELECT o.task_id,t.payload FROM local_outcomes o JOIN tasks t USING(task_id) "
                "WHERE o.outcome IN ('verified_success','dead_letter') ORDER BY o.task_id"
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload"] or "{}")
                lease = db.execute(
                    "SELECT status FROM leases WHERE task_id=?", (row["task_id"],)
                ).fetchone()
                if (lease is None or lease[0] != "active") and payload.get("generation_released", True) \
                        and payload.get("worktree_released", True):
                    eligible.append(row["task_id"])
            if apply:
                for task_id in eligible:
                    db.execute("DELETE FROM local_dependencies WHERE task_id=? OR depends_on=?",
                               (task_id, task_id))
                    db.execute("DELETE FROM local_outcomes WHERE task_id=?", (task_id,))
                    db.execute("DELETE FROM leases WHERE task_id=?", (task_id,))
                    db.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
        return {"schema": SCHEMA, "eligible": eligible, "removed": eligible if apply else []}
