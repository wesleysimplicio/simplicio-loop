# Hub durable retry queue (`HubRetryQueue`) runbook

Covers `simplicio_loop/hub_queue_retry.py` only — the SQLite WAL-backed durable
queue with idempotent submit, lease/visibility-timeout claiming, bounded retry
and dead-letter (DLQ). `HUB_RUNBOOK.md` covers the separate Hub IPC transport
(socket/pipe, lock file, `execute`); this file is scoped to the queue storage
layer itself, which as of this writing has no caller wired into `hub_daemon.py`
in this tree — it is a standalone class an operator or a future integration
constructs directly against a SQLite file path.

## Storage model (what actually exists on disk)

One SQLite database file at the path passed to `HubRetryQueue(path)`, opened
with `journal_mode=WAL` and `synchronous=FULL`. Two tables:

- `hub_jobs` — one row per task: `state` (`queued|leased|completed|dead_letter`),
  `attempts`/`max_attempts`, `lease_id`/`fence`/`lease_expires_at`, and the
  idempotency key with a `UNIQUE` constraint.
- `hub_dead_letters` — one row per task that exhausted `max_attempts`, carrying
  the typed `error_code` and the payload at time of death.

SQLite's own WAL mechanism produces `<path>-wal` and `<path>-shm` alongside the
main file. There is no explicit `PRAGMA wal_checkpoint` or `VACUUM` call
anywhere in this module — checkpointing is left to SQLite's default automatic
checkpoint (roughly every 1000 WAL pages, ~4 MB) plus whatever happens when a
connection closes.

## Failure modes given the current code

1. **Disk full / fsync failure.** `submit`, `claim`, `heartbeat`, `complete`,
   and `fail` all issue raw `self._db.execute(...)` calls with no
   `try/except` around `sqlite3.OperationalError` (e.g. `disk I/O error`,
   `database or disk is full`). The exception propagates uncaught to the
   caller. There is no retry-with-backoff or circuit breaker inside this
   module for that case.
2. **Unbounded table growth.** Completed rows in `hub_jobs` and resolved rows
   in `hub_dead_letters` are never pruned by this module. `requeue()` deletes
   a `hub_dead_letters` row when a dead task is explicitly requeued, but there
   is no retention policy, TTL sweep, or archival for `completed` rows. A
   long-lived queue file grows monotonically with total tasks ever submitted.
3. **WAL growth without checkpoints.** If a long-lived read transaction is
   held open elsewhere against the same file (e.g. an external `sqlite3` CLI
   session left open), SQLite cannot checkpoint the WAL, and `<path>-wal` can
   grow well past its usual size until that reader closes.
4. **WAL tail corruption / truncation.** `tests/test_hub_queue_retry.py::test_corrupt_wal_tail_fails_closed_and_keeps_last_valid_snapshot`
   verifies the actual behavior: if the final (uncommitted-at-the-time)
   transaction in `-wal` is truncated — e.g. the process was killed mid-write
   before the last commit reached durable storage — reopening the database
   silently drops that incomplete transaction and preserves every previously
   committed row. No task is duplicated and no already-committed task is
   lost; the queue keeps accepting new `submit()` calls afterward. This is
   SQLite's built-in WAL recovery, not custom logic in this module — if the
   `-wal`/main file is corrupted in a way SQLite itself cannot parse (not just
   a truncated tail), `HubRetryQueue.__init__` will raise `sqlite3.DatabaseError`
   ("file is not a database" or similar) and the queue is unusable until the
   file is restored from a backup or removed.
5. **No cross-host lease safety.** Leases use `time.time()` wall-clock
   comparisons within a single SQLite file. This is designed for one local
   Hub process; it gives no protection against clock skew if the file were
   ever shared across machines (it currently is not — it is local-only).
6. **Stuck dead-letter tasks require a manual, explicit action.** Once a task
   hits `max_attempts` it moves to `dead_letter` and stays there forever
   unless an operator calls `requeue(task_id)`. There is no automatic replay.

## Detecting degradation

- **Per-task state**: `HubRetryQueue(path).state(task_id)` returns
  `queued|leased|completed|dead_letter`, or raises `QueueRetryError` if the
  task_id is unknown.
- **Dead-letter backlog**: `HubRetryQueue(path).dead_letters()` returns every
  DLQ row with its typed `error_code` — a growing or non-empty list is the
  primary "queue is failing tasks" signal.
- **Direct inspection with the `sqlite3` CLI** (safe to run against a live
  file since SQLite readers do not block WAL writers):
  ```bash
  sqlite3 <path> "SELECT state, COUNT(*) FROM hub_jobs GROUP BY state;"
  sqlite3 <path> "SELECT task_id, error_code, attempts FROM hub_dead_letters;"
  ```
- **WAL growth**: `ls -la <path>-wal` — a `-wal` file that keeps growing well
  past a few MB without shrinking indicates checkpoints are not happening
  (long-held reader, or sustained write volume outpacing the default
  checkpoint interval).
- **Uncaught `sqlite3.OperationalError` / `sqlite3.DatabaseError` in logs**
  from any of `submit/claim/heartbeat/complete/fail/requeue/state` is the
  disk-full or corruption signal described above — these are not swallowed
  anywhere in this module, so they will surface directly in whatever calls
  `HubRetryQueue`.

## Rollback

`HubRetryQueue` has no feature flag of its own — nothing in this tree's
`hub_daemon.py` currently constructs one, so there is no live wiring to
disable. If it has been used directly (e.g. by a script or an in-progress
integration):

1. Stop the process(es) holding the SQLite connection open.
2. If the queue database is suspected corrupt beyond the tested WAL-tail-
   truncation recovery (i.e. `HubRetryQueue(path)` itself raises on open),
   move `<path>`, `<path>-wal`, and `<path>-shm` aside rather than deleting
   them, so the last good state is preserved for inspection, and start a
   fresh queue file at the same path.
3. Any caller that is integrated against this queue in the future should gate
   that call behind an explicit flag (env var or config) so "rollback" means
   flipping that flag off and falling back to whatever non-durable path
   existed before — no such flag exists yet because no caller exists yet in
   this tree.
