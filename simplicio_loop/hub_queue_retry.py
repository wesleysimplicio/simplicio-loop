"""Durable retry/dead-letter layer for the Hub queue.

Existing SQLiteRemoteQueue owns WAL, leases, and fencing. This focused layer
adds bounded retry state and an administrative DLQ without replacing that API.
"""

import json
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


QUEUE_SCHEMA = "simplicio.hub-queue/v1"


class QueueRetryError(RuntimeError):
    """Base durable retry error."""


class QueueLeaseError(QueueRetryError):
    """Raised for stale or missing task leases."""


class QueueCorruptionError(QueueRetryError):
    """Raised when the on-disk queue file fails SQLite's integrity check.

    Fail-closed rather than silently opening (and potentially further damaging) a corrupted
    file: the bad file is preserved alongside the original path (never overwritten or deleted)
    so it can be inspected/recovered, and the caller must decide how to proceed — e.g. restore
    from a separate backup, or start a fresh queue at a new path.
    """

    def __init__(self, message: str, *, preserved_path: str) -> None:
        super().__init__(message)
        self.preserved_path = preserved_path


@dataclass(frozen=True)
class RetryLease:
    task_id: str
    lease_id: str
    fence: int
    expires_at: float


class HubRetryQueue:
    """SQLite WAL queue with idempotent submit, bounded retry and DLQ."""

    def __init__(self, path: str) -> None:
        self.path = str(Path(path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._check_integrity_before_open()
        # #503 IPC wiring: HubDaemon's socket server handles each connection in its own
        # thread, all sharing this ONE HubRetryQueue/connection - check_same_thread=False
        # plus this RLock (below, wrapping every public method) makes that genuinely
        # safe, not just permitted. Multiple SEPARATE HubRetryQueue instances against the
        # same file from different threads (the existing concurrency tests' pattern)
        # remain valid too; this only adds safety for the shared-instance case.
        self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS hub_jobs (
                task_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                max_attempts INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'queued',
                next_attempt_at REAL NOT NULL,
                lease_id TEXT,
                fence INTEGER NOT NULL DEFAULT 0,
                lease_expires_at REAL,
                error_code TEXT,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hub_dead_letters (
                task_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                error_code TEXT NOT NULL,
                moved_at REAL NOT NULL
            );
            """
        )

    def _check_integrity_before_open(self) -> None:
        if not Path(self.path).exists():
            return  # fresh queue — nothing to check yet
        probe = sqlite3.connect(self.path, isolation_level=None)
        try:
            try:
                rows = probe.execute("PRAGMA integrity_check").fetchall()
            except sqlite3.DatabaseError as exc:
                # Not even a valid SQLite file (e.g. truncated/binary garbage) — integrity_check
                # itself cannot run.
                preserved = self._preserve_corrupt_file()
                raise QueueCorruptionError(
                    "hub queue file is not a valid SQLite database (%s); preserved at %s"
                    % (exc, preserved),
                    preserved_path=preserved,
                ) from exc
        finally:
            probe.close()
        results = [str(r[0]) for r in rows]
        if results != ["ok"]:
            preserved = self._preserve_corrupt_file()
            raise QueueCorruptionError(
                "hub queue file failed PRAGMA integrity_check (%s); preserved at %s"
                % ("; ".join(results), preserved),
                preserved_path=preserved,
            )

    def _preserve_corrupt_file(self) -> str:
        """Copy (never move/delete) the corrupted file + WAL/SHM sidecars aside for forensics.
        The original path is left untouched — the caller decides whether to remove it."""
        preserved = "%s.corrupt-%d" % (self.path, int(time.time() * 1000))
        Path(preserved).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.path, preserved)
        for suffix in ("-wal", "-shm"):
            sidecar = self.path + suffix
            if Path(sidecar).exists():
                shutil.copy2(sidecar, preserved + suffix)
        return preserved

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def submit(
        self,
        payload: Dict[str, Any],
        *,
        idempotency_key: str,
        max_attempts: int = 3,
    ) -> str:
        if not idempotency_key or max_attempts < 1:
            raise QueueRetryError("idempotency_key and positive max_attempts required")
        now = time.time()
        with self._lock:
            existing = self._db.execute(
                "SELECT task_id FROM hub_jobs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return str(existing["task_id"])
            task_id = str(uuid.uuid4())
            try:
                self._db.execute(
                    """
                    INSERT INTO hub_jobs(task_id,idempotency_key,payload,max_attempts,
                                         next_attempt_at,updated_at)
                    VALUES(?,?,?,?,?,?)
                    """,
                    (task_id, idempotency_key, json.dumps(payload, sort_keys=True),
                     int(max_attempts), now, now),
                )
            except sqlite3.IntegrityError:
                # A concurrent submit() with the same idempotency_key won the race between
                # our SELECT and INSERT (SQLite's UNIQUE constraint is what actually
                # serializes this across separate connections/processes - this lock only
                # protects concurrent THREADS sharing this one connection). Re-query rather
                # than raise so submit() stays idempotent under real concurrency.
                winner = self._db.execute(
                    "SELECT task_id FROM hub_jobs WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if winner is None:
                    raise
                return str(winner["task_id"])
            return task_id

    def claim(self, worker_id: str, *, ttl: float = 30.0) -> Optional[RetryLease]:
        if not worker_id or ttl <= 0:
            raise QueueRetryError("worker_id and positive ttl required")
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                # A task is claimable when it is freshly queued, OR when a prior
                # worker's lease visibility timeout has elapsed without heartbeat,
                # completion or failure (worker crash / hang). Without the second
                # branch a dead worker's lease would never be reclaimed.
                row = self._db.execute(
                    """
                    SELECT * FROM hub_jobs
                    WHERE (state='queued' AND next_attempt_at<=?)
                       OR (state='leased' AND lease_expires_at<=?)
                    ORDER BY updated_at, task_id LIMIT 1
                    """,
                    (now, now),
                ).fetchone()
                return self._claim_row(row, worker_id, ttl=ttl, now=now)
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def claim_specific(self, task_id: str, worker_id: str, *, ttl: float = 30.0) -> Optional[RetryLease]:
        """Claim exactly one named task rather than whatever `claim()` would pick next.

        Lets a caller that already decided WHICH task should run next (e.g. a fairness
        scheduler composed on top, per #505/#506 integration) hand that decision to the
        durable queue instead of re-picking by FIFO order. Same claimability rule and
        fencing as `claim()` — just filtered to one task_id.
        """
        if not task_id or not worker_id or ttl <= 0:
            raise QueueRetryError("task_id, worker_id and positive ttl required")
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    """
                    SELECT * FROM hub_jobs
                    WHERE task_id=? AND (
                      (state='queued' AND next_attempt_at<=?)
                      OR (state='leased' AND lease_expires_at<=?)
                    )
                    """,
                    (task_id, now, now),
                ).fetchone()
                return self._claim_row(row, worker_id, ttl=ttl, now=now)
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def _claim_row(self, row, worker_id: str, *, ttl: float, now: float) -> Optional[RetryLease]:
        """Shared claim body for `claim()`/`claim_specific()`: given a candidate row already
        selected under BEGIN IMMEDIATE, atomically fence-update it or fail closed. Caller's
        SELECT + this method together are one transaction; this always COMMITs or lets the
        caller's except-clause ROLLBACK."""
        if row is None:
            self._db.execute("COMMIT")
            return None
        lease_id = worker_id + "-" + uuid.uuid4().hex
        fence = int(row["fence"]) + 1
        expires = now + ttl
        cursor = self._db.execute(
            """
            UPDATE hub_jobs SET state='leased', attempts=attempts+1,
              lease_id=?, fence=?, lease_expires_at=?, updated_at=?
            WHERE task_id=? AND (state='queued' OR
              (state='leased' AND lease_expires_at<=? AND fence=?))
            """,
            (lease_id, fence, expires, now, row["task_id"], now, int(row["fence"])),
        )
        if cursor.rowcount == 0:
            # Lost a race with another claimant between the SELECT and the UPDATE;
            # fail closed instead of returning a lease that does not actually own the task.
            self._db.execute("COMMIT")
            return None
        self._db.execute("COMMIT")
        return RetryLease(str(row["task_id"]), lease_id, fence, expires)

    def get_payload(self, task_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM hub_jobs WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise QueueRetryError("unknown task")
            return json.loads(row["payload"])

    def _owned(self, lease: RetryLease) -> sqlite3.Row:
        row = self._db.execute(
            "SELECT * FROM hub_jobs WHERE task_id=?", (lease.task_id,)
        ).fetchone()
        if (
            row is None
            or row["state"] != "leased"
            or row["lease_id"] != lease.lease_id
            or int(row["fence"]) != lease.fence
            or row["lease_expires_at"] <= time.time()
        ):
            raise QueueLeaseError("lease is stale, expired, or missing")
        return row

    def heartbeat(self, lease: RetryLease, *, ttl: float = 30.0) -> RetryLease:
        if ttl <= 0:
            raise QueueRetryError("ttl must be positive")
        with self._lock:
            self._owned(lease)
            expires = time.time() + ttl
            self._db.execute(
                "UPDATE hub_jobs SET lease_expires_at=?,updated_at=? WHERE task_id=?",
                (expires, time.time(), lease.task_id),
            )
            return RetryLease(lease.task_id, lease.lease_id, lease.fence, expires)

    def complete(self, lease: RetryLease) -> None:
        with self._lock:
            self._owned(lease)
            self._db.execute(
                "UPDATE hub_jobs SET state='completed',updated_at=? WHERE task_id=?",
                (time.time(), lease.task_id),
            )

    def fail(self, lease: RetryLease, *, error_code: str, backoff: float = 0.0) -> str:
        if not error_code:
            raise QueueRetryError("error_code is required")
        with self._lock:
            row = self._owned(lease)
            now = time.time()
            if int(row["attempts"]) >= int(row["max_attempts"]):
                self._db.execute(
                    """
                    INSERT OR REPLACE INTO hub_dead_letters(task_id,payload,attempts,error_code,moved_at)
                    VALUES(?,?,?,?,?)
                    """,
                    (lease.task_id, row["payload"], row["attempts"], error_code, now),
                )
                self._db.execute(
                    "UPDATE hub_jobs SET state='dead_letter',error_code=?,updated_at=? WHERE task_id=?",
                    (error_code, now, lease.task_id),
                )
                return "dead_letter"
            self._db.execute(
                """
                UPDATE hub_jobs SET state='queued',next_attempt_at=?,error_code=?,
                  lease_id=NULL,lease_expires_at=NULL,updated_at=? WHERE task_id=?
                """,
                (now + max(0.0, backoff), error_code, now, lease.task_id),
            )
            return "retry"

    def dead_letters(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM hub_dead_letters ORDER BY moved_at, task_id"
            ).fetchall()
            return [dict(row) for row in rows]

    def requeue(self, task_id: str) -> None:
        with self._lock:
            row = self._db.execute(
                "SELECT state FROM hub_jobs WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None or row["state"] != "dead_letter":
                raise QueueRetryError("only dead-letter tasks can be requeued")
            self._db.execute(
                """
                UPDATE hub_jobs SET state='queued',next_attempt_at=?,error_code=NULL,
                  lease_id=NULL,lease_expires_at=NULL,updated_at=? WHERE task_id=?
                """,
                (time.time(), time.time(), task_id),
            )
            self._db.execute("DELETE FROM hub_dead_letters WHERE task_id=?", (task_id,))

    def state(self, task_id: str) -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT state FROM hub_jobs WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise QueueRetryError("unknown task")
            return str(row["state"])
