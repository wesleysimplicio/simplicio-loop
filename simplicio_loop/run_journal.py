"""Crash-safe RunJournal with deterministic, provider-free replay."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterable

EVENT_SCHEMA = "simplicio.run-event/v1"
TERMINAL_SCHEMA = "simplicio.run-terminal-receipt/v1"
GENESIS_HASH = "sha256:" + ("0" * 64)


class JournalError(RuntimeError):
    pass


class JournalIntegrityError(JournalError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


class RunJournal:
    """SQLite WAL event log whose writes are serialized and hash chained."""

    def __init__(
        self, path: str | Path, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.path = Path(path)
        self.clock = clock
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                str(self.path), timeout=30, isolation_level=None
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=30000")
            return connection
        except sqlite3.DatabaseError as exc:
            raise JournalIntegrityError(f"journal_open_failed:{exc}") from exc

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("BEGIN IMMEDIATE")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS run_events (
                        event_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        causal_parent TEXT,
                        idempotency_key TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        previous_hash TEXT NOT NULL,
                        event_hash TEXT NOT NULL,
                        UNIQUE(run_id, sequence),
                        UNIQUE(run_id, idempotency_key)
                    );
                    CREATE TABLE IF NOT EXISTS archived_events (
                        event_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        event_json TEXT NOT NULL,
                        UNIQUE(run_id, sequence)
                    );
                    CREATE TABLE IF NOT EXISTS run_heads (
                        run_id TEXT PRIMARY KEY,
                        sequence INTEGER NOT NULL,
                        event_id TEXT NOT NULL,
                        event_hash TEXT NOT NULL,
                        terminal_event_id TEXT
                    );
                    CREATE TABLE IF NOT EXISTS run_snapshots (
                        run_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        projection_json TEXT NOT NULL,
                        projection_hash TEXT NOT NULL,
                        verified INTEGER NOT NULL,
                        PRIMARY KEY(run_id, sequence)
                    );
                    CREATE TABLE IF NOT EXISTS journal_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    """
                )
                connection.execute(
                    "INSERT OR IGNORE INTO journal_meta(key,value) VALUES('schema_version','1')"
                )
                connection.commit()
        except sqlite3.DatabaseError as exc:
            raise JournalIntegrityError(f"journal_initialize_failed:{exc}") from exc
        self.assert_integrity()

    def assert_integrity(self) -> None:
        try:
            with self._connect() as connection:
                result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            raise JournalIntegrityError(f"integrity_check_failed:{exc}") from exc
        if result != "ok":
            raise JournalIntegrityError(f"integrity_check_failed:{result}")

    def _row_event(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": EVENT_SCHEMA,
            "event_id": row["event_id"],
            "run_id": row["run_id"],
            "sequence": row["sequence"],
            "kind": row["kind"],
            "causal_parent": row["causal_parent"],
            "idempotency_key": row["idempotency_key"],
            "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"],
            "previous_hash": row["previous_hash"],
            "event_hash": row["event_hash"],
        }

    def append(
        self,
        run_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        causal_parent: str | None = None,
        expected_sequence: int | None = None,
    ) -> dict[str, Any]:
        """Append once; duplicate keys return the original event."""
        self.assert_integrity()
        if not run_id.strip() or not kind.strip() or not idempotency_key.strip():
            raise ValueError("run_id, kind and idempotency_key are required")
        # Logical integrity (causal/hash chain) gates writes as strictly as the
        # physical SQLite integrity check.
        if self.events(run_id):
            self.replay(run_id)
        now = float(self.clock())
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                duplicate = connection.execute(
                    """SELECT * FROM run_events
                       WHERE run_id=? AND idempotency_key=?""",
                    (run_id, idempotency_key),
                ).fetchone()
                if duplicate is not None:
                    connection.commit()
                    return {
                        "status": "DUPLICATE",
                        "reason_code": "idempotency_key_replayed",
                        "event": self._row_event(duplicate),
                    }
                head = connection.execute(
                    "SELECT * FROM run_heads WHERE run_id=?", (run_id,)
                ).fetchone()
                if head is not None and head["terminal_event_id"] is not None:
                    connection.rollback()
                    return {
                        "status": "REJECTED",
                        "reason_code": "terminal_receipt_exists",
                        "terminal_event_id": head["terminal_event_id"],
                    }
                sequence = (int(head["sequence"]) if head else 0) + 1
                actual_parent = head["event_id"] if head else None
                if expected_sequence is not None and int(expected_sequence) != sequence:
                    connection.rollback()
                    return {
                        "status": "REJECTED",
                        "reason_code": "sequence_out_of_order",
                        "expected_sequence": sequence,
                    }
                if causal_parent is not None and causal_parent != actual_parent:
                    connection.rollback()
                    return {
                        "status": "REJECTED",
                        "reason_code": "causal_parent_mismatch",
                        "expected_parent": actual_parent,
                    }
                if sequence == 1 and kind != "run_started":
                    connection.rollback()
                    return {
                        "status": "REJECTED",
                        "reason_code": "run_not_started",
                    }
                previous_hash = head["event_hash"] if head else GENESIS_HASH
                event_id = f"{run_id}:{sequence}"
                body = {
                    "schema": EVENT_SCHEMA,
                    "event_id": event_id,
                    "run_id": run_id,
                    "sequence": sequence,
                    "kind": kind,
                    "causal_parent": actual_parent,
                    "idempotency_key": idempotency_key,
                    "payload": payload,
                    "created_at": now,
                    "previous_hash": previous_hash,
                }
                event_hash = _hash(body)
                connection.execute(
                    """INSERT INTO run_events(
                           event_id,run_id,sequence,kind,causal_parent,
                           idempotency_key,payload_json,created_at,
                           previous_hash,event_hash
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        event_id, run_id, sequence, kind, actual_parent,
                        idempotency_key, _canonical(payload), now,
                        previous_hash, event_hash,
                    ),
                )
                terminal = event_id if kind == "run_terminal" else None
                connection.execute(
                    """INSERT INTO run_heads(
                           run_id,sequence,event_id,event_hash,terminal_event_id
                       ) VALUES(?,?,?,?,?)
                       ON CONFLICT(run_id) DO UPDATE SET
                           sequence=excluded.sequence,event_id=excluded.event_id,
                           event_hash=excluded.event_hash,
                           terminal_event_id=COALESCE(
                               excluded.terminal_event_id,
                               run_heads.terminal_event_id
                           )""",
                    (run_id, sequence, event_id, event_hash, terminal),
                )
                connection.commit()
                return {
                    "status": "APPENDED",
                    "reason_code": None,
                    "event": {**body, "event_hash": event_hash},
                }
        except sqlite3.DatabaseError as exc:
            raise JournalIntegrityError(f"append_failed:{exc}") from exc

    def checkpoint_before_effect(
        self, run_id: str, effect_id: str, intent: dict[str, Any]
    ) -> dict[str, Any]:
        return self.append(
            run_id,
            "effect_prepared",
            {"effect_id": effect_id, "intent": intent},
            idempotency_key=f"effect:{effect_id}:prepared",
        )

    def checkpoint_after_effect(
        self, run_id: str, effect_id: str, receipt: dict[str, Any]
    ) -> dict[str, Any]:
        projection = self.replay(run_id)
        if effect_id not in projection["pending_effects"]:
            return {
                "status": "REJECTED",
                "reason_code": "effect_not_prepared",
            }
        return self.append(
            run_id,
            "effect_committed",
            {"effect_id": effect_id, "receipt": receipt},
            idempotency_key=f"effect:{effect_id}:committed",
        )

    def terminal(
        self, run_id: str, verdict: str, evidence: Iterable[str]
    ) -> dict[str, Any]:
        result = self.append(
            run_id,
            "run_terminal",
            {"verdict": verdict, "evidence": sorted(set(evidence))},
            idempotency_key="run:terminal",
        )
        if result["status"] not in {"APPENDED", "DUPLICATE"}:
            return result
        event = result["event"]
        receipt = {
            "schema": TERMINAL_SCHEMA,
            "run_id": run_id,
            "terminal_event_id": event["event_id"],
            "sequence": event["sequence"],
            "event_hash": event["event_hash"],
            "verdict": event["payload"]["verdict"],
            "evidence": event["payload"]["evidence"],
        }
        receipt["receipt_hash"] = _hash(receipt)
        return {
            "status": result["status"],
            "reason_code": result["reason_code"],
            "receipt": receipt,
        }

    def events(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            archived = connection.execute(
                """SELECT event_json FROM archived_events
                   WHERE run_id=? ORDER BY sequence""",
                (run_id,),
            ).fetchall()
            live = connection.execute(
                "SELECT * FROM run_events WHERE run_id=? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        return [json.loads(row["event_json"]) for row in archived] + [
            self._row_event(row) for row in live
        ]

    def replay(self, run_id: str) -> dict[str, Any]:
        """Pure reducer: no LLM, provider, network, environment or wall clock."""
        events = self.events(run_id)
        previous_hash, previous_id = GENESIS_HASH, None
        pending: dict[str, dict[str, Any]] = {}
        committed: dict[str, dict[str, Any]] = {}
        terminal = None
        for expected, event in enumerate(events, 1):
            body = {key: value for key, value in event.items() if key != "event_hash"}
            if (
                event["sequence"] != expected
                or event["causal_parent"] != previous_id
                or event["previous_hash"] != previous_hash
                or _hash(body) != event["event_hash"]
            ):
                raise JournalIntegrityError(
                    f"event_chain_invalid:{run_id}:{event['event_id']}"
                )
            payload = event["payload"]
            if event["kind"] == "effect_prepared":
                pending[payload["effect_id"]] = payload["intent"]
            elif event["kind"] == "effect_committed":
                effect_id = payload["effect_id"]
                if effect_id not in pending:
                    raise JournalIntegrityError(
                        f"effect_commit_without_prepare:{effect_id}"
                    )
                pending.pop(effect_id)
                committed[effect_id] = payload["receipt"]
            elif event["kind"] == "run_terminal":
                terminal = event
            previous_hash, previous_id = event["event_hash"], event["event_id"]
        return {
            "schema": "simplicio.run-projection/v1",
            "run_id": run_id,
            "sequence": len(events),
            "pending_effects": pending,
            "committed_effects": committed,
            "terminal": terminal,
            "head_hash": previous_hash,
            "replay_engine": "deterministic-python-reducer",
            "llm_used": False,
        }

    def snapshot_and_compact(self, run_id: str) -> dict[str, Any]:
        """Verify projection, archive events, and retain replay equivalence."""
        projection = self.replay(run_id)
        sequence = projection["sequence"]
        projection_hash = _hash(projection)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM run_events WHERE run_id=? ORDER BY sequence",
                (run_id,),
            ).fetchall()
            for row in rows:
                event = self._row_event(row)
                connection.execute(
                    """INSERT OR IGNORE INTO archived_events(
                           event_id,run_id,sequence,event_json
                       ) VALUES(?,?,?,?)""",
                    (event["event_id"], run_id, event["sequence"], _canonical(event)),
                )
            connection.execute(
                """INSERT OR REPLACE INTO run_snapshots(
                       run_id,sequence,projection_json,projection_hash,verified
                   ) VALUES(?,?,?,?,1)""",
                (run_id, sequence, _canonical(projection), projection_hash),
            )
            connection.execute("DELETE FROM run_events WHERE run_id=?", (run_id,))
            connection.commit()
        replayed = self.replay(run_id)
        if _hash(replayed) != projection_hash:
            raise JournalIntegrityError("post_compaction_replay_mismatch")
        return {
            "status": "COMPACTED",
            "run_id": run_id,
            "sequence": sequence,
            "projection_hash": projection_hash,
        }

    def migrate(
        self,
        target_version: int,
        migration: Callable[[sqlite3.Connection], None],
    ) -> dict[str, Any]:
        """Execute one migration atomically; exceptions roll back all DDL/DML."""
        self.assert_integrity()
        with self._connect() as connection:
            current = int(
                connection.execute(
                    "SELECT value FROM journal_meta WHERE key='schema_version'"
                ).fetchone()[0]
            )
            if target_version != current + 1:
                return {
                    "status": "REJECTED",
                    "reason_code": "migration_version_out_of_order",
                    "current_version": current,
                }
            try:
                connection.execute("BEGIN IMMEDIATE")
                migration(connection)
                connection.execute(
                    "UPDATE journal_meta SET value=? WHERE key='schema_version'",
                    (str(target_version),),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {"status": "MIGRATED", "version": target_version}

    def backup(self, target: str | Path) -> dict[str, Any]:
        self.assert_integrity()
        target = Path(target)
        with self._connect() as source, sqlite3.connect(str(target)) as destination:
            source.execute("PRAGMA wal_checkpoint(PASSIVE)")
            source.backup(destination)
        restored = RunJournal(target, clock=self.clock)
        restored.assert_integrity()
        return {
            "status": "BACKED_UP",
            "path": str(target),
            "sha256": "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest(),
        }

    @classmethod
    def restore(
        cls,
        backup_path: str | Path,
        target_path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> "RunJournal":
        source_journal = cls(backup_path, clock=clock)
        source_journal.assert_integrity()
        with source_journal._connect() as source, sqlite3.connect(
            str(target_path)
        ) as destination:
            source.backup(destination)
        restored = cls(target_path, clock=clock)
        restored.assert_integrity()
        return restored
