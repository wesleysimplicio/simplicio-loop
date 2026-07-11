"""Shared queue coordination with durable leases and fencing.

``SQLiteRemoteQueue`` is the development and single-host backend for the
``simplicio.queue/v1`` contract.  A SQLite file on a shared, transactional
volume can be used by multiple processes; deployments that need a network
service should implement :class:`RemoteQueue` with the same atomic methods.
The module intentionally has no fail-open path: an unavailable store raises
and callers must hand off rather than mutate a task.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Protocol


SCHEMA = "simplicio.queue/v1"


class QueueConflict(RuntimeError):
    """The caller lost a lease or presented an old fencing token."""


class QueueUnavailable(RuntimeError):
    """The queue could not be reached; mutation must pause and hand off."""


@dataclass(frozen=True)
class Lease:
    task_id: str
    agent_id: str
    lease_id: str
    fencing_token: int
    expires_at: float
    idempotency_key: str


class RemoteQueue(Protocol):
    def claim(self, task_id: str, agent_id: str, *, idempotency_key: str,
              ttl: float = 60.0) -> Lease: ...

    def heartbeat(self, lease: Lease, *, ttl: float = 60.0) -> Lease: ...

    def complete(self, lease: Lease, *, receipt_ref: str) -> Dict[str, Any]: ...


def _now() -> float:
    return time.time()


def _lease_id(task_id: str, agent_id: str, key: str) -> str:
    return hashlib.sha256((task_id + "\0" + agent_id + "\0" + key).encode()).hexdigest()[:32]


class SQLiteRemoteQueue:
    """Atomic queue backend suitable for local development and shared volumes.

    Every write is ``BEGIN IMMEDIATE`` and updates the monotonically increasing
    fencing token before returning a lease.  A stale worker cannot heartbeat,
    complete, or release after another worker has reclaimed the task.
    """

    def __init__(self, path: str, *, busy_timeout: float = 10.0) -> None:
        self.path = path
        self.busy_timeout = busy_timeout
        try:
            self._init()
        except sqlite3.Error as exc:
            raise QueueUnavailable("queue unavailable: %s" % exc) from exc

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.busy_timeout, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self) -> None:
        with self._connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS queue_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'ready',
                payload TEXT NOT NULL DEFAULT '{}', updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS leases (
                task_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, lease_id TEXT NOT NULL,
                fencing_token INTEGER NOT NULL, idempotency_key TEXT NOT NULL,
                expires_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                receipt_ref TEXT, updated_at REAL NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(task_id)
            );
            CREATE TABLE IF NOT EXISTS idempotency (
                idempotency_key TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                lease_id TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                kind TEXT NOT NULL, agent_id TEXT NOT NULL, fencing_token INTEGER,
                payload TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_task_seq ON events(task_id, seq);
            """)
            c.execute("INSERT OR IGNORE INTO queue_meta(key, value) VALUES('schema', ?)", (SCHEMA,))

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            c = self._connect()
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.commit()
            except Exception:
                c.rollback()
                raise
            finally:
                c.close()
        except sqlite3.OperationalError as exc:
            raise QueueUnavailable("queue unavailable: %s" % exc) from exc

    @staticmethod
    def _event(c: sqlite3.Connection, task_id: str, kind: str, agent_id: str,
               token: Optional[int], payload: Dict[str, Any]) -> None:
        c.execute("INSERT INTO events(task_id,kind,agent_id,fencing_token,payload,created_at) VALUES(?,?,?,?,?,?)",
                  (task_id, kind, agent_id, token, json.dumps(payload, sort_keys=True), _now()))

    def enqueue(self, task_id: str, payload: Optional[Dict[str, Any]] = None) -> None:
        task_id = str(task_id).strip()
        if not task_id:
            raise ValueError("task_id is required")
        with self._tx() as c:
            c.execute("INSERT OR IGNORE INTO tasks(task_id,status,payload,updated_at) VALUES(?,?,?,?)",
                      (task_id, "ready", json.dumps(payload or {}, sort_keys=True), _now()))
            self._event(c, task_id, "enqueued", "system", None, payload or {})

    def claim(self, task_id: str, agent_id: str, *, idempotency_key: str,
              ttl: float = 60.0) -> Lease:
        if ttl <= 0 or not agent_id or not idempotency_key:
            raise ValueError("agent_id, idempotency_key and positive ttl are required")
        now = _now()
        lid = _lease_id(task_id, agent_id, idempotency_key)
        try:
            with self._tx() as c:
                existing = c.execute("SELECT task_id,lease_id FROM idempotency WHERE idempotency_key=?",
                                     (idempotency_key,)).fetchone()
                if existing and existing["task_id"] != task_id:
                    raise QueueConflict("idempotency key already belongs to another task")
                if existing:
                    row = c.execute("SELECT * FROM leases WHERE task_id=? AND lease_id=?",
                                    (existing["task_id"], existing["lease_id"])).fetchone()
                    if row and row["status"] == "active" and row["expires_at"] > now:
                        return Lease(row["task_id"], row["agent_id"], row["lease_id"], row["fencing_token"],
                                     row["expires_at"], row["idempotency_key"])
                task = c.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if task is None:
                    raise KeyError("unknown task: %s" % task_id)
                if task["status"] == "completed":
                    raise QueueConflict("task already completed")
                current = c.execute("SELECT * FROM leases WHERE task_id=?", (task_id,)).fetchone()
                if current and current["status"] == "active" and current["expires_at"] > now:
                    raise QueueConflict("task already leased by %s" % current["agent_id"])
                token = int(current["fencing_token"]) + 1 if current else 1
                expires = now + ttl
                c.execute(
                    "INSERT OR REPLACE INTO leases(task_id,agent_id,lease_id,fencing_token,"
                    "idempotency_key,expires_at,status,receipt_ref,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                          (task_id, agent_id, lid, token, idempotency_key, expires, "active", None, now))
                c.execute(
                    "INSERT OR REPLACE INTO idempotency(idempotency_key,task_id,lease_id,created_at) "
                    "VALUES(?,?,?,?)",
                          (idempotency_key, task_id, lid, now))
                c.execute("UPDATE tasks SET status='claimed',updated_at=? WHERE task_id=?", (now, task_id))
                self._event(c, task_id, "claimed", agent_id, token, {"lease_id": lid, "expires_at": expires})
                return Lease(task_id, agent_id, lid, token, expires, idempotency_key)
        except sqlite3.Error as exc:
            raise QueueUnavailable("queue unavailable: %s" % exc) from exc

    def _owned(self, c: sqlite3.Connection, lease: Lease) -> sqlite3.Row:
        row = c.execute("SELECT * FROM leases WHERE task_id=?", (lease.task_id,)).fetchone()
        if (row is None or row["lease_id"] != lease.lease_id or row["agent_id"] != lease.agent_id or
                int(row["fencing_token"]) != lease.fencing_token or row["status"] != "active" or
                row["expires_at"] <= _now()):
            raise QueueConflict("stale or expired fencing token")
        return row

    def heartbeat(self, lease: Lease, *, ttl: float = 60.0) -> Lease:
        if ttl <= 0:
            raise ValueError("positive ttl is required")
        with self._tx() as c:
            self._owned(c, lease)
            expires = _now() + ttl
            c.execute("UPDATE leases SET expires_at=?,updated_at=? WHERE task_id=?", (expires, _now(), lease.task_id))
            self._event(c, lease.task_id, "heartbeat", lease.agent_id, lease.fencing_token, {"expires_at": expires})
            return Lease(lease.task_id, lease.agent_id, lease.lease_id, lease.fencing_token,
                         expires, lease.idempotency_key)

    def complete(self, lease: Lease, *, receipt_ref: str) -> Dict[str, Any]:
        if not receipt_ref:
            raise ValueError("receipt_ref is required")
        with self._tx() as c:
            self._owned(c, lease)
            now = _now()
            c.execute("UPDATE leases SET status='completed',receipt_ref=?,updated_at=? WHERE task_id=?",
                      (receipt_ref, now, lease.task_id))
            c.execute("UPDATE tasks SET status='completed',updated_at=? WHERE task_id=?", (now, lease.task_id))
            self._event(c, lease.task_id, "completed", lease.agent_id, lease.fencing_token,
                        {"receipt_ref": receipt_ref})
            return {"schema": SCHEMA, "task_id": lease.task_id, "status": "completed",
                    "fencing_token": lease.fencing_token, "receipt_ref": receipt_ref}

    def release(self, lease: Lease, *, reason: str = "handoff") -> Dict[str, Any]:
        with self._tx() as c:
            self._owned(c, lease)
            now = _now()
            c.execute("UPDATE leases SET status='released',updated_at=? WHERE task_id=?", (now, lease.task_id))
            c.execute("UPDATE tasks SET status='ready',updated_at=? WHERE task_id=?", (now, lease.task_id))
            self._event(c, lease.task_id, "released", lease.agent_id, lease.fencing_token, {"reason": reason})
            return {"task_id": lease.task_id, "status": "ready", "handoff": True, "reason": reason}

    def events(self, *, after: int = 0, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)).fetchall()
            return [{"seq": r["seq"], "task_id": r["task_id"], "kind": r["kind"],
                     "agent_id": r["agent_id"], "fencing_token": r["fencing_token"],
                     "payload": json.loads(r["payload"]), "created_at": r["created_at"]} for r in rows]

    def task(self, task_id: str) -> Dict[str, Any]:
        with self._connect() as c:
            row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            lease = c.execute("SELECT * FROM leases WHERE task_id=?", (task_id,)).fetchone()
            return {"task_id": task_id, "status": row["status"], "payload": json.loads(row["payload"]),
                    "lease": dict(lease) if lease else None}
