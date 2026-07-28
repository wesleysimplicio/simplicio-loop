from __future__ import annotations

import json
import multiprocessing
import sqlite3
from pathlib import Path

import pytest

from simplicio_loop.run_journal import JournalIntegrityError, RunJournal


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        value = self.value
        self.value += 1
        return value


def _concurrent_append(path, index, start, output):
    journal = RunJournal(path)
    start.wait()
    result = journal.append(
        "race", "observation", {"index": index}, idempotency_key=f"item:{index}"
    )
    output.put(result["status"])


def _started(journal, run_id="run-1"):
    return journal.append(
        run_id, "run_started", {"goal": "issue-808"}, idempotency_key="start"
    )


def test_fault_injection_at_effect_boundary_resumes_without_repeating(tmp_path):
    path = tmp_path / "journal.db"
    first = RunJournal(path, clock=Clock())
    _started(first)
    first.checkpoint_before_effect("run-1", "github:comment:1", {"body": "hello"})
    # Crash boundary: reopen without checkpoint_after_effect.
    resumed = RunJournal(path, clock=Clock(2000))
    projection = resumed.replay("run-1")
    assert projection["pending_effects"] == {
        "github:comment:1": {"body": "hello"}
    }
    committed = resumed.checkpoint_after_effect(
        "run-1", "github:comment:1", {"remote_id": 99}
    )
    duplicate = resumed.checkpoint_after_effect(
        "run-1", "github:comment:1", {"remote_id": 99}
    )
    assert committed["status"] == "APPENDED"
    assert duplicate["reason_code"] == "effect_not_prepared"
    assert resumed.replay("run-1")["committed_effects"] == {
        "github:comment:1": {"remote_id": 99}
    }


def test_duplicate_event_is_idempotent(tmp_path):
    journal = RunJournal(tmp_path / "journal.db", clock=Clock())
    first = _started(journal)
    duplicate = _started(journal)
    assert duplicate["status"] == "DUPLICATE"
    assert duplicate["event"]["event_hash"] == first["event"]["event_hash"]
    assert journal.replay("run-1")["sequence"] == 1


def test_out_of_order_has_stable_reason_codes(tmp_path):
    journal = RunJournal(tmp_path / "journal.db")
    rejected = journal.append(
        "run-1", "checkpoint", {}, idempotency_key="before-start"
    )
    assert rejected["reason_code"] == "run_not_started"
    _started(journal)
    rejected = journal.append(
        "run-1", "checkpoint", {}, idempotency_key="bad-sequence",
        expected_sequence=9,
    )
    assert rejected["reason_code"] == "sequence_out_of_order"
    rejected = journal.append(
        "run-1", "checkpoint", {}, idempotency_key="bad-parent",
        causal_parent="wrong",
    )
    assert rejected["reason_code"] == "causal_parent_mismatch"


def test_terminal_receipt_prevents_repetition(tmp_path):
    journal = RunJournal(tmp_path / "journal.db", clock=Clock())
    _started(journal)
    terminal = journal.terminal("run-1", "passed", ["test:green"])
    assert terminal["receipt"]["schema"] == "simplicio.run-terminal-receipt/v1"
    assert terminal["receipt"]["receipt_hash"].startswith("sha256:")
    blocked = journal.append(
        "run-1", "checkpoint", {}, idempotency_key="after-terminal"
    )
    assert blocked["reason_code"] == "terminal_receipt_exists"
    duplicate = journal.terminal("run-1", "passed", ["test:green"])
    assert duplicate["status"] == "DUPLICATE"


def test_terminal_receipt_fixture_is_reproducible(tmp_path):
    clock_values = iter([1000.0, 1001.0])
    journal = RunJournal(tmp_path / "journal.db", clock=clock_values.__next__)
    journal.append(
        "run-fixture", "run_started", {"goal": "fixture"},
        idempotency_key="start",
    )
    receipt = journal.terminal(
        "run-fixture", "passed", ["test:green"]
    )["receipt"]
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "run_terminal_receipt.json").read_text()
    )
    assert receipt == fixture


def test_wal_concurrency_has_contiguous_sequences(tmp_path):
    path = str(tmp_path / "journal.db")
    journal = RunJournal(path)
    journal.append("race", "run_started", {}, idempotency_key="start")
    ctx = multiprocessing.get_context("spawn")
    start, output = ctx.Event(), ctx.Queue()
    processes = [
        ctx.Process(target=_concurrent_append, args=(path, i, start, output))
        for i in range(12)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    assert [output.get(timeout=2) for _ in processes].count("APPENDED") == 12
    events = journal.events("race")
    assert [event["sequence"] for event in events] == list(range(1, 14))
    assert journal.replay("race")["sequence"] == 13


def test_migration_failure_rolls_back_schema_and_version(tmp_path):
    journal = RunJournal(tmp_path / "journal.db")

    def broken(connection):
        connection.execute("CREATE TABLE must_rollback(value TEXT)")
        raise RuntimeError("fault injected")

    with pytest.raises(RuntimeError, match="fault injected"):
        journal.migrate(2, broken)
    with journal._connect() as connection:
        version = connection.execute(
            "SELECT value FROM journal_meta WHERE key='schema_version'"
        ).fetchone()[0]
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE name='must_rollback'"
        ).fetchone()
    assert version == "1"
    assert table is None


def test_backup_restore_and_compaction_preserve_replay(tmp_path):
    original = RunJournal(tmp_path / "journal.db", clock=Clock())
    _started(original)
    original.checkpoint_before_effect("run-1", "effect-1", {"x": 1})
    original.checkpoint_after_effect("run-1", "effect-1", {"ok": True})
    expected = original.replay("run-1")
    compacted = original.snapshot_and_compact("run-1")
    assert compacted["status"] == "COMPACTED"
    assert original.replay("run-1") == expected

    backup = tmp_path / "backup.db"
    receipt = original.backup(backup)
    assert receipt["sha256"].startswith("sha256:")
    restored = RunJournal.restore(backup, tmp_path / "restored.db", clock=Clock())
    assert restored.replay("run-1") == expected


def test_tampered_event_chain_fails_closed(tmp_path):
    path = tmp_path / "journal.db"
    journal = RunJournal(path, clock=Clock())
    _started(journal)
    journal.append(
        "run-1", "checkpoint", {"safe": True}, idempotency_key="checkpoint"
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE run_events SET payload_json='{}' WHERE sequence=2"
        )
    with pytest.raises(JournalIntegrityError, match="event_chain_invalid"):
        journal.replay("run-1")
    with pytest.raises(JournalIntegrityError, match="event_chain_invalid"):
        journal.append(
            "run-1", "checkpoint", {}, idempotency_key="must-not-write"
        )


def test_corrupted_database_blocks_open_and_writes(tmp_path):
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"not-a-sqlite-database")
    with pytest.raises(JournalIntegrityError):
        RunJournal(path)
