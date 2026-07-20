"""Durable retry/dead-letter layer for the Hub queue.

Existing SQLiteRemoteQueue owns WAL, leases, and fencing. This focused layer
adds bounded retry state and an administrative DLQ without replacing that API.
"""

import hashlib
import json
import math
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


QUEUE_SCHEMA = "simplicio.hub-queue/v1"
ADMISSION_RECEIPT_SCHEMA = "simplicio.hub-admission-receipt/v1"


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
            CREATE TABLE IF NOT EXISTS hub_admissions (
                task_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                input_digest TEXT NOT NULL,
                job TEXT NOT NULL,
                client_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                weight INTEGER NOT NULL,
                cost INTEGER NOT NULL,
                receipt TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        self._migrate_scheduling_columns()

    def _migrate_scheduling_columns(self) -> None:
        """#503-506 restart persistence: add the scheduling metadata (client_id,
        workspace_id, weight, cost) needed to rehydrate a FairScheduler after a daemon
        restart. ADD COLUMN, not a fresh CREATE TABLE, so a queue file created before
        this change keeps its existing durable rows - a real migration, not a reset."""
        existing = {row["name"] for row in self._db.execute("PRAGMA table_info(hub_jobs)").fetchall()}
        additions = (
            ("client_id", "TEXT NOT NULL DEFAULT ''"),
            ("workspace_id", "TEXT NOT NULL DEFAULT 'default'"),
            ("weight", "INTEGER NOT NULL DEFAULT 1"),
            ("cost", "INTEGER NOT NULL DEFAULT 1"),
        )
        for name, ddl in additions:
            if name in existing:
                continue
            try:
                self._db.execute("ALTER TABLE hub_jobs ADD COLUMN %s %s" % (name, ddl))
            except sqlite3.OperationalError as exc:
                # A real race, found by the existing multi-connection concurrency test:
                # two HubRetryQueue instances opening the same file at nearly the same
                # moment can both see the column missing and both try to add it. The
                # second one loses - benign (the schema already has what it needs),
                # not a real failure.
                if "duplicate column name" not in str(exc):
                    raise
        self._backfill_legacy_client_ids()

    @staticmethod
    def _effective_client_id(payload: Any, explicit_client_id: Any) -> str:
        """Return the durable client identity for a new queue row.

        An explicit, nonempty string is authoritative.  Older callers only placed
        that identity in the payload, so use a valid payload object as a fallback.
        Deliberately do not stringify arbitrary values: scheduler identity must not
        be invented from malformed input.
        """
        if isinstance(explicit_client_id, str) and explicit_client_id:
            return explicit_client_id
        if isinstance(payload, dict):
            payload_client_id = payload.get("client_id")
            if isinstance(payload_client_id, str) and payload_client_id:
                return payload_client_id
        return ""

    def _backfill_legacy_client_ids(self) -> None:
        """Populate only missing legacy identities without changing queue metadata."""
        rows = self._db.execute(
            "SELECT task_id,payload FROM hub_jobs WHERE client_id=''"
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(str(row["payload"]))
            except (TypeError, ValueError):
                continue
            client_id = self._effective_client_id(payload, "")
            if not client_id:
                continue
            self._db.execute(
                "UPDATE hub_jobs SET client_id=? WHERE task_id=? AND client_id=''",
                (client_id, row["task_id"]),
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
        client_id: Any = "",
        workspace_id: str = "default",
        weight: int = 1,
        cost: int = 1,
    ) -> str:
        if not idempotency_key or max_attempts < 1:
            raise QueueRetryError("idempotency_key and positive max_attempts required")
        now = time.time()
        with self._lock:
            existing = self._db.execute(
                "SELECT task_id,state FROM hub_jobs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if str(existing["state"]) == "admitted_held":
                    raise QueueRetryError("held admission cannot be submitted")
                return str(existing["task_id"])
            effective_client_id = self._effective_client_id(payload, client_id)
            task_id = str(uuid.uuid4())
            try:
                self._db.execute(
                    """
                    INSERT INTO hub_jobs(task_id,idempotency_key,payload,max_attempts,
                                         next_attempt_at,updated_at,client_id,workspace_id,
                                         weight,cost)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (task_id, idempotency_key, json.dumps(payload, sort_keys=True),
                     int(max_attempts), now, now, effective_client_id, str(workspace_id),
                     int(weight), int(cost)),
                )
            except sqlite3.IntegrityError:
                # A concurrent submit() with the same idempotency_key won the race between
                # our SELECT and INSERT (SQLite's UNIQUE constraint is what actually
                # serializes this across separate connections/processes - this lock only
                # protects concurrent THREADS sharing this one connection). Re-query rather
                # than raise so submit() stays idempotent under real concurrency.
                winner = self._db.execute(
                    "SELECT task_id,state FROM hub_jobs WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if winner is None:
                    raise
                if str(winner["state"]) == "admitted_held":
                    raise QueueRetryError("held admission cannot be submitted")
                return str(winner["task_id"])
            return task_id

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _value_digest(cls, value: Any) -> str:
        return hashlib.sha256(cls._canonical_json(value).encode("utf-8")).hexdigest()

    def _after_held_job_insert(self, task_id: str) -> None:
        """Fault-injection seam; production intentionally does nothing."""

    def _validate_held_input(
        self, job: Dict[str, Any], *, idempotency_key: str, input_digest: str,
        client_id: str, workspace_id: str, weight: int, cost: int,
    ) -> None:
        from .github_drain_admission import (
            DrainAdmissionProjectionError, admission_idempotency_key, admission_input_digest,
            validate_admission_metadata, validate_projected_job,
        )
        if not isinstance(job, dict):
            raise QueueRetryError("held admission job must be an object")
        try:
            validate_projected_job(job)
        except (DrainAdmissionProjectionError, TypeError, ValueError) as exc:
            raise QueueRetryError("held admission job projection is invalid") from exc
        try:
            validate_admission_metadata(
                client_id=client_id, workspace_id=workspace_id, weight=weight, cost=cost,
            )
            expected_key = admission_idempotency_key(job)
            expected_digest = admission_input_digest(
                job, client_id=client_id, workspace_id=workspace_id, weight=weight, cost=cost,
            )
        except (DrainAdmissionProjectionError, TypeError, ValueError) as exc:
            raise QueueRetryError("held admission identity/metadata is invalid") from exc
        if (
            idempotency_key != expected_key or input_digest != expected_digest
        ):
            raise QueueRetryError("held admission identity/input is invalid")

    @staticmethod
    def _valid_nonnegative_counts(value: Any, keys: Set[str]) -> bool:
        return (
            isinstance(value, dict) and set(value) == keys
            and all(isinstance(item, int) and not isinstance(item, bool) and item >= 0 for item in value.values())
        )

    def _validate_capacity_snapshot(self, snapshot: Dict[str, Any]) -> None:
        from .hub_governor import RESOURCE_NAMES
        scheduler = snapshot.get("scheduler") if isinstance(snapshot, dict) else None
        governor = snapshot.get("governor") if isinstance(snapshot, dict) else None
        scheduler_limit_keys = {
            "max_inflight_per_client", "max_queue_per_client", "max_queue_per_workspace",
            "max_global_queue", "quantum", "aging_ticks", "aging_boost",
        }
        circuit_keys = {"state", "failures", "threshold", "cooldown_seconds"}
        valid_limits = (
            isinstance(scheduler, dict) and isinstance(scheduler.get("limits"), dict)
            and set(scheduler["limits"]) == scheduler_limit_keys
            and all(
                isinstance(scheduler["limits"][name], int)
                and not isinstance(scheduler["limits"][name], bool)
                and scheduler["limits"][name] >= 1
                for name in {"max_inflight_per_client", "quantum", "aging_ticks", "aging_boost"}
            )
            and all(
                scheduler["limits"][name] is None
                or (
                    isinstance(scheduler["limits"][name], int)
                    and not isinstance(scheduler["limits"][name], bool)
                    and scheduler["limits"][name] >= 1
                )
                for name in {"max_queue_per_client", "max_queue_per_workspace", "max_global_queue"}
            )
        )
        circuit = governor.get("circuit") if isinstance(governor, dict) else None
        valid_circuit = (
            isinstance(circuit, dict) and set(circuit) == circuit_keys
            and circuit.get("state") in {"closed", "open", "half_open"}
            and isinstance(circuit.get("failures"), int) and not isinstance(circuit.get("failures"), bool)
            and circuit.get("failures") >= 0
            and isinstance(circuit.get("threshold"), int) and not isinstance(circuit.get("threshold"), bool)
            and circuit.get("threshold") >= 1
            and isinstance(circuit.get("cooldown_seconds"), (int, float))
            and not isinstance(circuit.get("cooldown_seconds"), bool)
            and circuit.get("cooldown_seconds") >= 0
        )
        valid_governor = (
            isinstance(governor, dict)
            and set(governor) == {"limits", "used", "target_client_used", "draining", "circuit"}
            and self._valid_nonnegative_counts(governor.get("limits"), set(RESOURCE_NAMES))
            and self._valid_nonnegative_counts(governor.get("used"), set(RESOURCE_NAMES))
            and self._valid_nonnegative_counts(governor.get("target_client_used"), set(RESOURCE_NAMES))
            and isinstance(governor.get("draining"), bool)
            and valid_circuit
        )
        if not (
            isinstance(snapshot, dict)
            and set(snapshot) == {
                "schema", "reservation", "fresh_snapshot_required_at_activation",
                "scheduler", "governor",
            }
            and snapshot.get("schema") == "simplicio.hub-capacity-observation/v1"
            and snapshot.get("reservation") is False
            and snapshot.get("fresh_snapshot_required_at_activation") is True
            and isinstance(scheduler, dict)
            and set(scheduler) == {"limits", "global", "target_client", "target_workspace"}
            and valid_limits
            and self._valid_nonnegative_counts(scheduler.get("global"), {"queued", "global_total", "clients"})
            and self._valid_nonnegative_counts(scheduler.get("target_client"), {"total", "inflight"})
            and self._valid_nonnegative_counts(scheduler.get("target_workspace"), {"total"})
            and valid_governor
        ):
            raise QueueRetryError("capacity snapshot is invalid or unsanitized")

    def _decode_admission_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        try:
            receipt = json.loads(str(row["receipt"]))
            job = json.loads(str(row["job"]))
        except (TypeError, ValueError) as exc:
            raise QueueRetryError("stored admission receipt is invalid") from exc
        if not isinstance(receipt, dict) or not isinstance(job, dict):
            raise QueueRetryError("stored admission receipt is invalid")
        from .github_drain_admission import (
            DrainAdmissionProjectionError, admission_idempotency_key, admission_input_digest,
            validate_projected_job,
        )
        queued = self._db.execute(
            "SELECT * FROM hub_jobs WHERE task_id=?", (str(row["task_id"]),)
        ).fetchone()
        receipt_payload = {key: value for key, value in receipt.items() if key != "receipt_hash"}
        receipt_keys = {
            "schema", "task_id", "idempotency_key", "input_digest", "state", "recovery",
            "execution_authorized", "capacity_snapshot", "created_at", "receipt_hash",
        }
        created_at = receipt.get("created_at")
        stored_created_at = row["created_at"]
        try:
            created_at_valid = (
                isinstance(created_at, str)
                and len(created_at) == 20
                and isinstance(stored_created_at, (int, float))
                and not isinstance(stored_created_at, bool)
                and math.isfinite(float(stored_created_at))
                and time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
                ) == created_at
                and time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(stored_created_at))
                ) == created_at
            )
        except (TypeError, ValueError, OverflowError, OSError):
            created_at_valid = False
        try:
            validate_projected_job(job)
            expected_key = admission_idempotency_key(job)
            expected_digest = admission_input_digest(
                job, client_id=str(row["client_id"]), workspace_id=str(row["workspace_id"]),
                weight=int(row["weight"]), cost=int(row["cost"]),
            )
        except DrainAdmissionProjectionError as exc:
            raise QueueRetryError("stored admission identity is invalid") from exc
        if (
            queued is None or str(queued["state"]) != "admitted_held"
            or str(queued["idempotency_key"]) != str(row["idempotency_key"])
            or str(queued["payload"]) != str(row["job"])
            or str(queued["client_id"]) != str(row["client_id"])
            or str(queued["workspace_id"]) != str(row["workspace_id"])
            or int(queued["weight"]) != int(row["weight"])
            or int(queued["cost"]) != int(row["cost"])
            or expected_key != str(row["idempotency_key"])
            or expected_digest != str(row["input_digest"])
            or receipt.get("schema") != ADMISSION_RECEIPT_SCHEMA
            or receipt.get("task_id") != str(row["task_id"])
            or receipt.get("idempotency_key") != str(row["idempotency_key"])
            or receipt.get("input_digest") != str(row["input_digest"])
            or receipt.get("state") != "admitted_held"
            or receipt.get("recovery") != "ADMITTED_NOT_DISPATCHED"
            or receipt.get("execution_authorized") is not False
            or set(receipt) != receipt_keys
            or not created_at_valid
            or receipt.get("receipt_hash") != self._value_digest(receipt_payload)
        ):
            raise QueueRetryError("stored admission receipt failed validation")
        self._validate_capacity_snapshot(receipt.get("capacity_snapshot"))
        return receipt

    def admit_held(
        self,
        job: Dict[str, Any],
        *,
        idempotency_key: str,
        input_digest: str,
        client_id: str,
        workspace_id: str = "default",
        weight: int = 1,
        cost: int = 1,
        capacity_snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Atomically persist one held job and its immutable admission receipt."""
        self._validate_held_input(
            job, idempotency_key=idempotency_key, input_digest=input_digest,
            client_id=client_id, workspace_id=workspace_id, weight=weight, cost=cost,
        )
        self._validate_capacity_snapshot(capacity_snapshot)
        job_json = self._canonical_json(job)
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                queued = self._db.execute(
                    "SELECT * FROM hub_jobs WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                existing = self._db.execute(
                    "SELECT * FROM hub_admissions WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if queued is not None or existing is not None:
                    if queued is None or existing is None or str(queued["state"]) != "admitted_held":
                        raise QueueRetryError("idempotency key collides with a non-admission job")
                    if (
                        str(existing["input_digest"]) != input_digest
                        or str(existing["job"]) != job_json
                        or str(existing["client_id"]) != client_id
                        or str(existing["workspace_id"]) != workspace_id
                        or int(existing["weight"]) != weight or int(existing["cost"]) != cost
                        or str(queued["payload"]) != job_json
                        or str(queued["client_id"]) != client_id
                        or str(queued["workspace_id"]) != workspace_id
                        or int(queued["weight"]) != weight or int(queued["cost"]) != cost
                    ):
                        raise QueueRetryError("idempotency key conflicts with different held input")
                    receipt = self._decode_admission_row(existing)
                    self._db.execute("COMMIT")
                    return receipt

                task_id = str(uuid.uuid4())
                self._db.execute(
                    """
                    INSERT INTO hub_jobs(task_id,idempotency_key,payload,max_attempts,attempts,
                                         state,next_attempt_at,updated_at,client_id,workspace_id,
                                         weight,cost)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (task_id, idempotency_key, job_json, 1, 0, "admitted_held", now, now,
                     client_id, workspace_id, weight, cost),
                )
                self._after_held_job_insert(task_id)
                receipt: Dict[str, Any] = {
                    "schema": ADMISSION_RECEIPT_SCHEMA,
                    "task_id": task_id,
                    "idempotency_key": idempotency_key,
                    "input_digest": input_digest,
                    "state": "admitted_held",
                    "recovery": "ADMITTED_NOT_DISPATCHED",
                    "execution_authorized": False,
                    "capacity_snapshot": dict(capacity_snapshot),
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                }
                receipt["receipt_hash"] = self._value_digest(receipt)
                receipt_json = self._canonical_json(receipt)
                self._db.execute(
                    """
                    INSERT INTO hub_admissions(task_id,idempotency_key,input_digest,job,
                                               client_id,workspace_id,weight,cost,receipt,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (task_id, idempotency_key, input_digest, job_json, client_id,
                     workspace_id, weight, cost, receipt_json, now),
                )
                self._db.execute("COMMIT")
                return receipt
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def admission(
        self, *, task_id: str = "", idempotency_key: str = ""
    ) -> Dict[str, Any]:
        if bool(task_id) == bool(idempotency_key):
            raise QueueRetryError("exactly one of task_id or idempotency_key is required")
        column, value = ("task_id", task_id) if task_id else ("idempotency_key", idempotency_key)
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM hub_admissions WHERE %s=?" % column, (value,)
            ).fetchone()
            if row is None:
                raise QueueRetryError("unknown held admission")
            return self._decode_admission_row(row)

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

    def list_queued_scheduling_metadata(self) -> List[Dict[str, Any]]:
        """#503-506 restart persistence: enough per-task info (task_id, client_id,
        workspace_id, weight, cost) to rehydrate a FairScheduler's in-memory queues
        after a daemon restart. Only genuinely still-queued or expired-leased
        (effectively-queued) tasks - never leased/completed/dead_letter, which the
        scheduler should not re-admit."""
        now = time.time()
        with self._lock:
            rows = self._db.execute(
                """
                SELECT task_id, client_id, workspace_id, weight, cost FROM hub_jobs
                WHERE (state='queued' AND next_attempt_at<=?)
                   OR (state='leased' AND lease_expires_at<=?)
                ORDER BY updated_at, task_id
                """,
                (now, now),
            ).fetchall()
            return [
                {
                    "task_id": str(row["task_id"]),
                    "client_id": str(row["client_id"]),
                    "workspace_id": str(row["workspace_id"]),
                    "weight": int(row["weight"]),
                    "cost": int(row["cost"]),
                }
                for row in rows
            ]

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
            now = time.time()
            cursor = self._db.execute(
                """UPDATE hub_jobs SET lease_expires_at=?,updated_at=?
                   WHERE task_id=? AND lease_id=? AND fence=?
                     AND state='leased' AND lease_expires_at>?""",
                (expires, now, lease.task_id, lease.lease_id, lease.fence, now),
            )
            if cursor.rowcount == 0:
                raise QueueLeaseError("lease is stale, expired, or missing")
            return RetryLease(lease.task_id, lease.lease_id, lease.fence, expires)

    def complete(self, lease: RetryLease) -> None:
        with self._lock:
            self._owned(lease)
            now = time.time()
            cursor = self._db.execute(
                """UPDATE hub_jobs SET state='completed',updated_at=?
                   WHERE task_id=? AND lease_id=? AND fence=?
                     AND state='leased' AND lease_expires_at>?""",
                (now, lease.task_id, lease.lease_id, lease.fence, now),
            )
            if cursor.rowcount == 0:
                raise QueueLeaseError("lease is stale, expired, or missing")

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
                cursor = self._db.execute(
                    """UPDATE hub_jobs SET state='dead_letter',error_code=?,updated_at=?
                       WHERE task_id=? AND lease_id=? AND fence=?
                         AND state='leased' AND lease_expires_at>?""",
                    (error_code, now, lease.task_id, lease.lease_id, lease.fence, now),
                )
                if cursor.rowcount == 0:
                    raise QueueLeaseError("lease is stale, expired, or missing")
                return "dead_letter"
            cursor = self._db.execute(
                """
                UPDATE hub_jobs SET state='queued',next_attempt_at=?,error_code=?,
                  lease_id=NULL,lease_expires_at=NULL,updated_at=?
                WHERE task_id=? AND lease_id=? AND fence=?
                  AND state='leased' AND lease_expires_at>?
                """,
                (now + max(0.0, backoff), error_code, now, lease.task_id,
                 lease.lease_id, lease.fence, now),
            )
            if cursor.rowcount == 0:
                raise QueueLeaseError("lease is stale, expired, or missing")
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

    # Compatibility helpers used by the daemon's scheduler-backed IPC path.
    def find_task_id(self, idempotency_key: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute(
                "SELECT task_id FROM hub_jobs WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            return str(row["task_id"]) if row is not None else None

    def get_row(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM hub_jobs WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            data = dict(row)
            data["payload"] = json.loads(data["payload"])
            return data

    def update_payload(self, task_id: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            row = self._db.execute(
                "SELECT state FROM hub_jobs WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise QueueRetryError("unknown task")
            if str(row["state"]) == "admitted_held":
                raise QueueRetryError("held admission payload is immutable")
            self._db.execute(
                "UPDATE hub_jobs SET payload=?,updated_at=? WHERE task_id=?",
                (json.dumps(payload, sort_keys=True), time.time(), task_id),
            )

    def count(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) AS n FROM hub_jobs").fetchone()
            return int(row["n"])

    def payload_of(self, task_id: str) -> Dict[str, Any]:
        return self.get_payload(task_id)

    def sync_fair_scheduler(self, scheduler: Any) -> None:
        """Admit durable queued rows that are not already represented in a scheduler."""
        from .hub_scheduler import ScheduledJob, SchedulerError

        for entry in self.list_queued_scheduling_metadata():
            try:
                scheduler.enqueue(ScheduledJob(
                    task_id=entry["task_id"], client_id=entry["client_id"] or "default",
                    weight=entry["weight"], cost=entry["cost"],
                    workspace_id=entry["workspace_id"],
                ))
            except SchedulerError:
                continue

    def claim_fair(self, scheduler: Any, worker_id: str, *, ttl: float = 30.0,
                   max_attempts: int = 256) -> Optional[RetryLease]:
        self.sync_fair_scheduler(scheduler)
        for _ in range(max_attempts):
            scheduled = scheduler.next()
            if scheduled is None:
                return None
            lease = self.claim_specific(scheduled.task_id, worker_id, ttl=ttl)
            try:
                scheduler.complete(scheduled.task_id)
            except Exception:
                pass
            if lease is not None:
                return lease
        return None
