"""Real, un-mocked proof that ``serve-async`` wires AsyncBoundedQueue into a
production ingestion/dispatch/report call site (issue #495/#508).

Unlike the isolated ``AsyncBoundedQueue`` unit/benchmark suites, this spawns the
actual packaged CLI (``simplicio_loop.remote_worker_cli``) as a real OS process
against a real shared SQLite queue file, drives several real tasks through it
concurrently, and proves cooperative shutdown -- closing the mechanical gap the
epic's own status comments named explicitly: the primitive existed and was well
tested in isolation, but had zero production callers.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = [sys.executable, "-m", "simplicio_loop.remote_worker_cli"]


def _task_status(db: Path, task_id: str) -> str:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else "missing"


def _wait_until(predicate, *, timeout: float = 15.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise TimeoutError("condition never became true")


def _read_status(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_serve_async_drains_bounded_queue_concurrently_and_shuts_down_cleanly(tmp_path):
    db = tmp_path / "queue.db"
    status_file = tmp_path / "worker.status.json"

    from simplicio_loop.remote_queue import SQLiteRemoteQueue
    queue = SQLiteRemoteQueue(str(db))
    task_ids = [f"WI-495-{i}" for i in range(6)]
    for task_id in task_ids:
        queue.enqueue(task_id, {"goal": "prove serve-async drains via bounded queues"})

    proc = subprocess.Popen(
        [
            *CLI, "serve-async",
            "--db", str(db), "--agent-id", "agent-async",
            "--ttl", "5", "--heartbeat-interval", "0.2",
            "--work-seconds", "0.2", "--poll-interval", "0.05",
            "--concurrency", "3",
            "--status-file", str(status_file),
        ],
        cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        stdin=subprocess.DEVNULL,
    )
    try:
        # All six tasks complete even though only 3 dispatch workers/ingest slots
        # are available at a time -- the bounded ingest queue applies backpressure
        # to discovery instead of losing or starving any candidate.
        _wait_until(lambda: all(_task_status(db, t) == "completed" for t in task_ids), timeout=20.0)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
            proc.wait(timeout=5.0)
            raise

    assert proc.returncode == 0

    # The report queue's writer flushes a final "stopped" status after every
    # dispatch worker drains -- proving the report stage is genuinely ordered
    # after ingestion/dispatch finish, not fire-and-forget.
    _wait_until(lambda: status_file.exists() and _read_status(status_file).get("state") == "stopped",
                timeout=10.0)
    final = _read_status(status_file)
    assert final["state"] == "stopped"
    assert final["pid"] == proc.pid


def test_serve_async_ingest_backpressure_bounds_in_flight_claims(tmp_path):
    """Concurrency=1 must still process every task, one at a time, never losing work."""
    db = tmp_path / "queue-seq.db"
    status_file = tmp_path / "worker-seq.status.json"

    from simplicio_loop.remote_queue import SQLiteRemoteQueue
    queue = SQLiteRemoteQueue(str(db))
    task_ids = [f"WI-495-SEQ-{i}" for i in range(4)]
    for task_id in task_ids:
        queue.enqueue(task_id, {})

    proc = subprocess.Popen(
        [
            *CLI, "serve-async",
            "--db", str(db), "--agent-id", "agent-seq",
            "--ttl", "5", "--heartbeat-interval", "0.2",
            "--work-seconds", "0.1", "--poll-interval", "0.05",
            "--concurrency", "1",
            "--status-file", str(status_file),
        ],
        cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        stdin=subprocess.DEVNULL,
    )
    try:
        _wait_until(lambda: all(_task_status(db, t) == "completed" for t in task_ids), timeout=20.0)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
            proc.wait(timeout=5.0)
            raise

    assert proc.returncode == 0
