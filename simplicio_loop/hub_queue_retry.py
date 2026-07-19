"""Durable retry/dead-letter layer for the Hub queue.

Existing SQLiteRemoteQueue owns WAL, leases, and fencing. This focused layer
adds bounded retry state and an administrative DLQ without replacing that API.
"""

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .hub_scheduler import FairScheduler, ScheduledJob, SchedulerError


QUEUE_SCHEMA = "simplicio.hub-queue/v1"


class QueueRetryError(RuntimeError):
    """Base durable retry error."""


class QueueLeaseError(QueueRetryError):
    """Raised for stale or missing task leases."""


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
        self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
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

    def close(self) -> None:
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
            # A concurrent submit() with the same idempotency_key won the race between our
            # SELECT and INSERT (SQLite's UNIQUE constraint is what actually serializes this,
            # not the SELECT above). Re-query rather than raise so submit() stays idempotent
            # under real concurrency, not just when calls happen to be serialized.
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
                # Lost a race with another claimant between the SELECT and
                # the UPDATE; fail closed instead of returning a lease that
                # does not actually own the task.
                self._db.execute("COMMIT")
                return None
            self._db.execute("COMMIT")
            return RetryLease(str(row["task_id"]), lease_id, fence, expires)
        except Exception:
            self._db.execute("ROLLBACK")
            raise

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
        self._owned(lease)
        expires = time.time() + ttl
        self._db.execute(
            "UPDATE hub_jobs SET lease_expires_at=?,updated_at=? WHERE task_id=?",
            (expires, time.time(), lease.task_id),
        )
        return RetryLease(lease.task_id, lease.lease_id, lease.fence, expires)

    def complete(self, lease: RetryLease) -> None:
        self._owned(lease)
        self._db.execute(
            "UPDATE hub_jobs SET state='completed',updated_at=? WHERE task_id=?",
            (time.time(), lease.task_id),
        )

    def fail(self, lease: RetryLease, *, error_code: str, backoff: float = 0.0) -> str:
        if not error_code:
            raise QueueRetryError("error_code is required")
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
        rows = self._db.execute(
            "SELECT * FROM hub_dead_letters ORDER BY moved_at, task_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def requeue(self, task_id: str) -> None:
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
        row = self._db.execute(
            "SELECT state FROM hub_jobs WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise QueueRetryError("unknown task")
        return str(row["state"])

    def find_task_id(self, idempotency_key: str) -> Optional[str]:
        row = self._db.execute(
            "SELECT task_id FROM hub_jobs WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        return str(row["task_id"]) if row is not None else None

    def get_row(self, task_id: str) -> Optional[Dict[str, Any]]:
        row = self._db.execute(
            "SELECT * FROM hub_jobs WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["payload"] = json.loads(data["payload"])
        return data

    def update_payload(self, task_id: str, payload: Dict[str, Any]) -> None:
        self._db.execute(
            "UPDATE hub_jobs SET payload=?,updated_at=? WHERE task_id=?",
            (json.dumps(payload, sort_keys=True), time.time(), task_id),
        )

    def count(self) -> int:
        row = self._db.execute("SELECT COUNT(*) AS n FROM hub_jobs").fetchone()
        return int(row["n"])

    def payload_of(self, task_id: str) -> Dict[str, Any]:
        row = self._db.execute(
            "SELECT payload FROM hub_jobs WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise QueueRetryError("unknown task")
        return dict(json.loads(row["payload"]))

    def _claim_specific(self, task_id: str, worker_id: str, *, ttl: float = 30.0) -> Optional[RetryLease]:
        if not worker_id or ttl <= 0:
            raise QueueRetryError("worker_id and positive ttl required")
        now = time.time()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute(
                "SELECT * FROM hub_jobs WHERE task_id=? AND state='queued' AND next_attempt_at<=?",
                (task_id, now),
            ).fetchone()
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
                WHERE task_id=? AND state='queued'
                """,
                (lease_id, fence, expires, now, task_id),
            )
            if cursor.rowcount == 0:
                self._db.execute("COMMIT")
                return None
            self._db.execute("COMMIT")
            return RetryLease(task_id, lease_id, fence, expires)
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def sync_fair_scheduler(self, scheduler: FairScheduler) -> None:
        """Enqueue every ready-but-untracked task into `scheduler` so its DRR/quota
        ordering decision covers the real durable backlog, not an in-memory mock.

        Callers that also track their own submit-time scheduler.enqueue() (e.g.
        HubDaemon, for backpressure) will see a benign duplicate-task_id SchedulerError
        for jobs it already knows about; this loop swallows exactly that and moves on.
        Only safe when the row-level `state` column tracks real queued/leased/completed
        lifecycle for that caller's jobs (see HubDaemon's own claim_next, which does
        NOT call this for that reason).
        """
        now = time.time()
        rows = self._db.execute(
            "SELECT task_id, payload FROM hub_jobs WHERE state='queued' AND next_attempt_at<=?",
            (now,),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            try:
                scheduler.enqueue(
                    ScheduledJob(
                        task_id=str(row["task_id"]),
                        client_id=str(payload.get("client_id") or "default"),
                        weight=int(payload.get("weight", 1)),
                        cost=int(payload.get("cost", 1)),
                        workspace_id=str(payload.get("workspace_id") or "default"),
                    )
                )
            except SchedulerError:
                continue

    def claim_fair(
        self, scheduler: FairScheduler, worker_id: str, *, ttl: float = 30.0, max_attempts: int = 256
    ) -> Optional[RetryLease]:
        """Claim the next task chosen by `scheduler`'s fairness order, backed by this
        durable queue's real lease/fencing state (not just the scheduler's in-memory view)."""
        self.sync_fair_scheduler(scheduler)
        for _ in range(max_attempts):
            scheduled = scheduler.next()
            if scheduled is None:
                return None
            lease = self._claim_specific(scheduled.task_id, worker_id, ttl=ttl)
            try:
                scheduler.complete(scheduled.task_id)
            except SchedulerError:
                pass
            if lease is not None:
                return lease
        return None
