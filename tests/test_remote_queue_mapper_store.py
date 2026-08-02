from __future__ import annotations

import sqlite3
import threading

from simplicio_loop.remote_queue import SQLiteRemoteQueue


def test_compatibility_queue_has_no_legacy_queue_tables(tmp_path):
    queue = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    queue.enqueue("task-1", {"kind": "mapper"})
    with sqlite3.connect(queue.path) as database:
        tables = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert not {"queue_meta", "tasks", "leases", "idempotency", "events"} & tables
    assert {"ops_tasks", "ops_attempts", "ops_leases", "ops_events"}.issubset(tables)


def test_concurrent_compatibility_initialization_is_mapper_owned(tmp_path):
    path = str(tmp_path / "queue.db")
    barrier = threading.Barrier(6)
    errors: list[BaseException] = []

    def initialize() -> None:
        try:
            barrier.wait(timeout=5)
            SQLiteRemoteQueue(path)
        except BaseException as error:  # pragma: no cover - assertion below reports it
            errors.append(error)

    threads = [threading.Thread(target=initialize) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors


def test_queue_events_replay_from_mapper_operations_journal(tmp_path):
    queue = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    queue.enqueue("task-1")
    lease = queue.claim("task-1", "worker", idempotency_key="task-1")
    queue.complete(lease, receipt_ref="receipts/task-1.json")
    events = queue.events()
    assert [event["kind"] for event in events] == ["enqueued", "claimed", "completed"]
    replay = queue._mapper.operations.replay(queue._run_id)
    assert replay["valid"] is True
