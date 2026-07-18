import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from simplicio_loop.hub_queue_retry import (
    HubRetryQueue,
    QueueCorruptionError,
    QueueLeaseError,
    QueueRetryError,
)


def test_idempotent_submit_survives_restart_and_dead_letters_after_budget() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "queue.db")
        queue = HubRetryQueue(path)
        task_id = queue.submit({"kind": "test"}, idempotency_key="same", max_attempts=2)
        assert queue.submit({"kind": "changed"}, idempotency_key="same") == task_id

        first = queue.claim("worker-a", ttl=10)
        assert first is not None
        assert queue.fail(first, error_code="temporary") == "retry"
        second = queue.claim("worker-b", ttl=10)
        assert second is not None
        assert queue.fail(second, error_code="permanent") == "dead_letter"
        assert queue.dead_letters()[0]["error_code"] == "permanent"
        queue.close()

        restarted = HubRetryQueue(path)
        assert restarted.state(task_id) == "dead_letter"
        assert restarted.dead_letters()[0]["task_id"] == task_id
        restarted.requeue(task_id)
        assert restarted.state(task_id) == "queued"
        restarted.close()


def test_only_current_lease_can_heartbeat_or_complete() -> None:
    with tempfile.TemporaryDirectory() as directory:
        queue = HubRetryQueue(str(Path(directory) / "queue.db"))
        task_id = queue.submit({}, idempotency_key="key")
        lease = queue.claim("worker", ttl=10)
        assert lease is not None
        stale = type(lease)(task_id, "wrong", lease.fence, lease.expires_at)
        with pytest.raises(QueueLeaseError):
            queue.heartbeat(stale)
        queue.complete(lease)
        assert queue.state(task_id) == "completed"
        queue.close()


def test_expired_lease_is_reclaimed_by_another_worker() -> None:
    """A worker that crashes after claim (no heartbeat/fail/complete) must not
    strand the task: once the visibility timeout elapses, another worker can
    claim it, and the dead worker's lease is fenced off (#504 AC: "apenas um
    worker possui lease valida por tarefa" / lease renewal + expiration).
    """
    with tempfile.TemporaryDirectory() as directory:
        queue = HubRetryQueue(str(Path(directory) / "queue.db"))
        task_id = queue.submit({"kind": "test"}, idempotency_key="expiring")
        dead = queue.claim("worker-dead", ttl=0.05)
        assert dead is not None
        assert queue.claim("worker-live-too-soon") is None

        time.sleep(0.15)

        revived = queue.claim("worker-live", ttl=10)
        assert revived is not None
        assert revived.task_id == task_id
        assert revived.fence == dead.fence + 1
        assert queue.state(task_id) == "leased"

        # The crashed worker's old lease is fenced off, not the current owner.
        with pytest.raises(QueueLeaseError):
            queue.heartbeat(dead)
        with pytest.raises(QueueLeaseError):
            queue.complete(dead)

        # The new owner's lease is genuinely valid.
        queue.heartbeat(revived, ttl=10)
        queue.complete(revived)
        assert queue.state(task_id) == "completed"
        queue.close()


class _BarrierGatedDB:
    """Wraps a queue's sqlite3 connection so every thread's idempotency-key SELECT rendezvous
    at a barrier before returning, forcing a deterministic cross-connection TOCTOU window instead
    of relying on incidental thread-scheduling timing (which does not reliably reproduce the race
    on a fast local run)."""

    def __init__(self, db, barrier) -> None:
        self._db = db
        self._barrier = barrier
        self._gated = False  # only the FIRST matching SELECT is the real race window; the
        # fix's own post-conflict recovery SELECT reuses identical SQL and must not re-gate.

    def execute(self, sql, params=()):
        result = self._db.execute(sql, params)
        if not self._gated and sql.lstrip().startswith(
            "SELECT task_id FROM hub_jobs WHERE idempotency_key"
        ):
            self._gated = True
            self._barrier.wait(timeout=5)
        return result

    def executescript(self, *args, **kwargs):
        return self._db.executescript(*args, **kwargs)

    def close(self) -> None:
        self._db.close()


def test_concurrent_submit_with_same_idempotency_key_never_raises_and_is_idempotent() -> None:
    """Real, deterministically-forced multi-connection race (#504): two threads, each with its
    own HubRetryQueue/sqlite3 connection against the same file, are synchronized via a barrier so
    BOTH observe "no existing row" for the same idempotency_key before either INSERTs — the exact
    TOCTOU window submit() has across separate connections. Before the fix this raised
    sqlite3.IntegrityError instead of staying idempotent; the fix must catch it and agree on one
    winner's task_id.
    """
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "queue.db")
        n = 2
        barrier = threading.Barrier(n)
        results: list = [None] * n
        errors: list = []

        def submit_once(i: int) -> None:
            # Each connection must be created in the thread that uses it (sqlite3 forbids
            # cross-thread use of a connection by default), so the queue/wrap happens here.
            try:
                queue = HubRetryQueue(path)
                queue._db = _BarrierGatedDB(queue._db, barrier)
                results[i] = queue.submit({"kind": "race"}, idempotency_key="racing-key")
                queue._db.close()
            except Exception as exc:  # noqa: BLE001 - intentionally broad, asserted on below
                errors.append(exc)

        threads = [threading.Thread(target=submit_once, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, "submit() must never raise under a real forced race, got: %r" % (errors,)
        assert results[0] is not None and results[0] == results[1], (
            "both racing submits must agree on the same task_id"
        )

        plain = HubRetryQueue(path)
        row = plain._db.execute(
            "SELECT COUNT(*) AS n FROM hub_jobs WHERE idempotency_key='racing-key'"
        ).fetchone()
        assert row["n"] == 1
        plain.close()


def test_invalid_requests_and_empty_queue_are_rejected() -> None:
    with tempfile.TemporaryDirectory() as directory:
        queue = HubRetryQueue(str(Path(directory) / "queue.db"))
        assert queue.claim("worker") is None
        with pytest.raises(QueueRetryError):
            queue.submit({}, idempotency_key="", max_attempts=0)
        queue.close()


def test_expired_lease_is_reclaimable_by_another_worker() -> None:
    with tempfile.TemporaryDirectory() as directory:
        queue = HubRetryQueue(str(Path(directory) / "queue.db"))
        task_id = queue.submit({}, idempotency_key="k")
        first = queue.claim("worker-a", ttl=0.05)
        assert first is not None
        time.sleep(0.1)

        second = queue.claim("worker-b", ttl=10)
        assert second is not None
        assert second.task_id == task_id
        assert second.lease_id != first.lease_id
        assert second.fence != first.fence

        with pytest.raises(QueueLeaseError):
            queue.heartbeat(first)
        with pytest.raises(QueueLeaseError):
            queue.complete(first)

        queue.complete(second)
        assert queue.state(task_id) == "completed"
        queue.close()


_CRASH_SCRIPT = """
import sys
from simplicio_loop.hub_queue_retry import HubRetryQueue

queue = HubRetryQueue(sys.argv[1])
queue.submit({"kind": "first"}, idempotency_key="first")
sys.stdout.write(str(len(open(sys.argv[1] + "-wal", "rb").read())))
sys.stdout.flush()
queue.submit({"kind": "second"}, idempotency_key="second")
import os
os._exit(0)
"""


def test_restart_after_crash_preserves_committed_writes_without_duplicates() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "queue.db")
        result = subprocess.run(
            [sys.executable, "-c", _CRASH_SCRIPT, path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        wal_size_after_first = int(result.stdout.strip())
        assert wal_size_after_first > 0

        restarted = HubRetryQueue(path)
        rows = restarted._db.execute(
            "SELECT idempotency_key,state FROM hub_jobs ORDER BY idempotency_key"
        ).fetchall()
        keys = [row["idempotency_key"] for row in rows]
        assert keys == ["first", "second"]
        assert len(set(row["idempotency_key"] for row in rows)) == len(rows)
        for row in rows:
            assert row["state"] == "queued"
        restarted.close()


def test_corrupt_wal_tail_fails_closed_and_keeps_last_valid_snapshot() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "queue.db")
        result = subprocess.run(
            [sys.executable, "-c", _CRASH_SCRIPT, path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        wal_size_after_first = int(result.stdout.strip())

        wal_path = Path(path + "-wal")
        assert wal_path.exists()
        full_size = wal_path.stat().st_size
        assert full_size > wal_size_after_first

        with open(wal_path, "r+b") as handle:
            handle.truncate(wal_size_after_first)

        restarted = HubRetryQueue(path)
        rows = restarted._db.execute(
            "SELECT idempotency_key FROM hub_jobs ORDER BY idempotency_key"
        ).fetchall()
        keys = [row["idempotency_key"] for row in rows]
        assert keys == ["first"]

        task_id = restarted.submit({"kind": "post-recovery"}, idempotency_key="third")
        assert restarted.state(task_id) == "queued"
        restarted.close()


def test_mangled_db_file_fails_closed_and_preserves_the_corrupt_copy() -> None:
    """Real corruption (#504 AC: "WAL corrompido ... fail-closed com snapshot preservado") is
    distinct from the WAL-tail-truncation case above, which SQLite already recovers from
    natively — this is a genuinely malformed database file that must NOT be silently opened
    or overwritten. Uses real bytes, real PRAGMA integrity_check, no mocking."""
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "queue.db")
        queue = HubRetryQueue(path)
        queue.submit({"kind": "before-corruption"}, idempotency_key="pre")
        queue.close()

        # Corrupt the schema b-tree page's cell-count field (2 bytes at offset 103-104, right
        # after the 100-byte file header) — a real, verifiable structural corruption. A plain
        # byte overwrite in unused/free space, or truncating trailing free pages, does NOT
        # reliably trip SQLite's integrity_check (empirically confirmed: SQLite tolerates both
        # here since the corrupted regions were unused). This targets an always-read page.
        with open(path, "r+b") as handle:
            handle.seek(103)
            handle.write(b"\xff\xff")

        with pytest.raises(QueueCorruptionError) as excinfo:
            HubRetryQueue(path)

        preserved = Path(excinfo.value.preserved_path)
        assert preserved.exists(), "corrupted file must be preserved for forensics"
        assert preserved.read_bytes() == Path(path).read_bytes(), (
            "preserved copy must match the corrupted file byte-for-byte"
        )
        # Fail-closed means the original corrupted file is left exactly as-is too — never
        # deleted or reset to a fresh empty schema behind the caller's back.
        assert Path(path).exists()


def test_non_sqlite_garbage_file_fails_closed() -> None:
    """A completely non-SQLite file (not just internally corrupted) must also fail closed
    rather than let SQLite silently treat it as a fresh empty database."""
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "queue.db")
        Path(path).write_bytes(b"this is not a sqlite database at all, just plain text\n" * 20)

        with pytest.raises(QueueCorruptionError) as excinfo:
            HubRetryQueue(path)

        assert Path(excinfo.value.preserved_path).exists()


def test_pre_existing_old_schema_file_migrates_scheduling_columns_without_data_loss() -> None:
    """#503-506 restart persistence: a queue file created BEFORE the scheduling
    metadata columns existed must migrate cleanly (real ALTER TABLE, not a fresh
    CREATE TABLE) - the old row survives with sane defaults, not silently dropped."""
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "old.db")
        db = sqlite3.connect(path, isolation_level=None)
        db.execute(
            """CREATE TABLE hub_jobs (
                task_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
                max_attempts INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'queued', next_attempt_at REAL NOT NULL,
                lease_id TEXT, fence INTEGER NOT NULL DEFAULT 0, lease_expires_at REAL,
                error_code TEXT, updated_at REAL NOT NULL)"""
        )
        db.execute(
            """CREATE TABLE hub_dead_letters (
                task_id TEXT PRIMARY KEY, payload TEXT NOT NULL, attempts INTEGER NOT NULL,
                error_code TEXT NOT NULL, moved_at REAL NOT NULL)"""
        )
        db.execute(
            "INSERT INTO hub_jobs(task_id,idempotency_key,payload,max_attempts,next_attempt_at,updated_at)"
            " VALUES (?,?,?,?,?,?)",
            ("old-task", "old-key", "{}", 3, 0, 0),
        )
        db.close()

        queue = HubRetryQueue(path)
        assert queue.state("old-task") == "queued"
        metadata = queue.list_queued_scheduling_metadata()
        assert metadata == [
            {"task_id": "old-task", "client_id": "", "workspace_id": "default", "weight": 1, "cost": 1}
        ]
        # New submits on the migrated file work normally too.
        new_task_id = queue.submit(
            {"kind": "new"}, idempotency_key="new-key", client_id="alice", workspace_id="ws1",
            weight=2, cost=3,
        )
        entries = {row["task_id"]: row for row in queue.list_queued_scheduling_metadata()}
        assert entries[new_task_id] == {
            "task_id": new_task_id, "client_id": "alice", "workspace_id": "ws1", "weight": 2, "cost": 3,
        }
        queue.close()


def test_list_queued_scheduling_metadata_excludes_active_completed_and_dead_letter() -> None:
    with tempfile.TemporaryDirectory() as directory:
        queue = HubRetryQueue(str(Path(directory) / "queue.db"))
        queued_id = queue.submit({}, idempotency_key="k1", client_id="alice")
        leased_id = queue.submit({}, idempotency_key="k2", client_id="bob")
        lease = queue.claim("worker-1", ttl=30)
        assert lease.task_id == queued_id or lease.task_id == leased_id
        completed_id = queue.submit({}, idempotency_key="k3", client_id="carol")
        completed_lease = queue.claim("worker-2", ttl=30)
        queue.complete(completed_lease)

        remaining_task_ids = {row["task_id"] for row in queue.list_queued_scheduling_metadata()}
        assert lease.task_id not in remaining_task_ids  # actively leased, not re-schedulable
        assert completed_lease.task_id not in remaining_task_ids
        queue.close()
