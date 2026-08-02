from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from simplicio_loop import local_task_queue_cli as cli


class FakeMapperQueue:
    def __init__(self, database: Path) -> None:
        self.database = database
        self.calls: list[tuple[str, dict, str]] = []

    def submit(self, task_id, payload, *, idempotency_key, priority=0):
        self.calls.append((task_id, dict(payload), idempotency_key))
        return {"status": "queued", "task_id": task_id, "priority": priority}


def _legacy_queue(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE tasks(task_id TEXT PRIMARY KEY,status TEXT NOT NULL,payload TEXT,updated_at REAL)")
        db.executemany(
            "INSERT INTO tasks VALUES(?,?,?,?)",
            [
                ("ready", "ready", json.dumps({"kind": "work"}), 1.0),
                ("done", "completed", json.dumps({"kind": "history"}), 2.0),
                ("running", "claimed", json.dumps({"kind": "active"}), 3.0),
            ],
        )


def test_mapper_queue_migration_is_read_only_in_plan_and_hashes_backup_on_apply(tmp_path, monkeypatch):
    source = tmp_path / "queue.sqlite3"
    _legacy_queue(source)
    monkeypatch.setattr(cli, "_legacy_queue_path", lambda _repo: source)
    destination = FakeMapperQueue(tmp_path / "operations.sqlite")

    planned = cli._migrate_legacy_queue("ignored", destination, apply=False)
    assert planned["status"] == "planned"
    assert planned["counts"] == {"source": 3, "imported": 1, "skipped": 2}
    assert planned["backup"] is None
    assert destination.calls == []

    applied = cli._migrate_legacy_queue("ignored", destination, apply=True)
    assert applied["status"] == "applied"
    assert applied["backup"]
    backup = Path(applied["backup"])
    assert backup.is_file()
    assert applied["backup_sha256"] == hashlib.sha256(backup.read_bytes()).hexdigest()
    assert destination.calls[0][0] == "ready"
    assert destination.calls[0][2] == "legacy-loop:ready"
    assert {item["reason"] for item in applied["skipped"]} == {
        "terminal_history_requires_receipt_import",
        "active_or_unknown_state_requires_reconciliation",
    }
