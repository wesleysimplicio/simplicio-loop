"""Durable, atomic idempotency/effect ledger for the Hookwall boundary."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from .hookwall_gate import (
    HookwallBlocked, validate_pre_decision, verify_post_receipt,
)

SCHEMA = "simplicio.hookwall-ledger/v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class HookwallEffectLedger:
    """Single-writer SQLite ledger that never guesses whether an effect ran."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS hookwall_effects (
                idempotency_key TEXT PRIMARY KEY,
                envelope_hash TEXT NOT NULL,
                envelope_json TEXT NOT NULL,
                pre_json TEXT NOT NULL,
                state TEXT NOT NULL,
                effect_hash TEXT,
                receipt_json TEXT,
                post_json TEXT,
                evidence_json TEXT,
                reason_code TEXT,
                updated_ns INTEGER NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS hookwall_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                previous_event_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL,
                created_ns INTEGER NOT NULL
            )""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @staticmethod
    def _append_event(db: sqlite3.Connection, key: str, event_type: str,
                      payload: Mapping[str, Any]) -> str:
        previous_row = db.execute(
            "SELECT event_hash FROM hookwall_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous = str(previous_row["event_hash"]) if previous_row else "0" * 64
        event = {
            "idempotency_key": key, "event_type": event_type,
            "payload": dict(payload), "previous_event_hash": previous,
        }
        event_hash = _hash(event)
        db.execute(
            "INSERT INTO hookwall_events(idempotency_key,event_type,payload_json,"
            "previous_event_hash,event_hash,created_ns) VALUES(?,?,?,?,?,?)",
            (key, event_type, _canonical(payload), previous, event_hash, time.time_ns()),
        )
        return event_hash

    def reserve(self, envelope: Mapping[str, Any],
                pre_decision: Mapping[str, Any]) -> dict[str, Any]:
        env = validate_pre_decision(envelope, pre_decision)
        key = str(env["idempotency_key"])
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM hookwall_effects WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row is not None:
                if row["envelope_hash"] != env["envelope_hash"]:
                    self._append_event(db, key, "REPLAY_BLOCKED", {
                        "reason_code": "idempotency_lineage_mismatch",
                        "observed_envelope_hash": env["envelope_hash"],
                    })
                    db.execute("COMMIT")
                    raise HookwallBlocked(
                        "idempotency_lineage_mismatch",
                        "idempotency key belongs to another envelope",
                    )
                state = str(row["state"])
                if state == "VERIFIED":
                    db.execute("COMMIT")
                    return {
                        "action": "REPLAY_VERIFIED", "state": state,
                        "evidence": json.loads(row["evidence_json"]),
                    }
                self._append_event(db, key, "RETRY_BLOCKED", {
                    "reason_code": "effect_reconciliation_required", "state": state,
                })
                db.execute("COMMIT")
                raise HookwallBlocked(
                    "effect_reconciliation_required",
                    f"prior transaction is {state}; effect will not be replayed",
                )
            db.execute(
                "INSERT INTO hookwall_effects VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (key, env["envelope_hash"], _canonical(env), _canonical(pre_decision),
                 "RESERVED", None, None, None, None, None, time.time_ns()),
            )
            event_hash = self._append_event(
                db, key, "RESERVED",
                {"envelope_hash": env["envelope_hash"], "fence": env["fence"]},
            )
            db.execute("COMMIT")
            return {"action": "EXECUTE", "state": "RESERVED",
                    "event_hash": event_hash}

    def effect_confirmed(self, key: str, result: Mapping[str, Any]) -> dict[str, Any]:
        effect_hash = _hash(result)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state,effect_hash FROM hookwall_effects WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                raise HookwallBlocked("hookwall_bypass", "effect has no reservation")
            if row["state"] == "EFFECT_CONFIRMED" and row["effect_hash"] == effect_hash:
                db.execute("COMMIT")
                return {"state": "EFFECT_CONFIRMED", "effect_hash": effect_hash}
            if row["state"] != "RESERVED":
                db.execute("ROLLBACK")
                raise HookwallBlocked("invalid_effect_transition", str(row["state"]))
            db.execute(
                "UPDATE hookwall_effects SET state='EFFECT_CONFIRMED',effect_hash=?,"
                "updated_ns=? WHERE idempotency_key=?",
                (effect_hash, time.time_ns(), key),
            )
            event_hash = self._append_event(
                db, key, "EFFECT_CONFIRMED", {"effect_hash": effect_hash}
            )
            db.execute("COMMIT")
            return {"state": "EFFECT_CONFIRMED", "effect_hash": effect_hash,
                    "event_hash": event_hash}

    def verify_and_commit(self, envelope: Mapping[str, Any],
                          pre_decision: Mapping[str, Any],
                          receipt: Mapping[str, Any],
                          post_decision: Mapping[str, Any]) -> dict[str, Any]:
        evidence = verify_post_receipt(
            envelope, pre_decision, receipt, post_decision
        )
        key = str(envelope["idempotency_key"])
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state FROM hookwall_effects WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row is None or row["state"] != "EFFECT_CONFIRMED":
                db.execute("ROLLBACK")
                raise HookwallBlocked(
                    "effect_not_persisted", "post gate requires confirmed effect journal"
                )
            event_hash = self._append_event(
                db, key, "VERIFIED",
                {"receipt_hash": receipt["receipt_hash"],
                 "evidence_hash": evidence["evidence_hash"]},
            )
            result = dict(evidence)
            result["ledger_event_hash"] = event_hash
            result["evidence_hash"] = _hash({
                field: value for field, value in result.items()
                if field != "evidence_hash"
            })
            db.execute(
                "UPDATE hookwall_effects SET state='VERIFIED',receipt_json=?,post_json=?,"
                "evidence_json=?,updated_ns=? WHERE idempotency_key=?",
                (_canonical(receipt), _canonical(post_decision), _canonical(result),
                 time.time_ns(), key),
            )
            db.execute("COMMIT")
        return result

    def mark_unresolved(self, key: str, reason_code: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state FROM hookwall_effects WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row is None:
                self._append_event(db, key, "BYPASS_BLOCKED", {
                    "reason_code": reason_code
                })
                db.execute("COMMIT")
                return {"state": "BLOCKED", "reason_code": reason_code}
            target = "UNCERTAIN" if row["state"] in {"RESERVED", "EFFECT_CONFIRMED"} else row["state"]
            db.execute(
                "UPDATE hookwall_effects SET state=?,reason_code=?,updated_ns=? "
                "WHERE idempotency_key=?",
                (target, reason_code, time.time_ns(), key),
            )
            event_hash = self._append_event(
                db, key, target, {"reason_code": reason_code}
            )
            db.execute("COMMIT")
            return {"state": target, "reason_code": reason_code,
                    "event_hash": event_hash}

    def verify_audit_chain(self) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM hookwall_events ORDER BY sequence"
            ).fetchall()
        previous = "0" * 64
        for expected_sequence, row in enumerate(rows, start=1):
            if row["sequence"] != expected_sequence or row["previous_event_hash"] != previous:
                return {"schema": SCHEMA, "status": "INVALID",
                        "reason_code": "event_chain_broken",
                        "verified_events": expected_sequence - 1}
            payload = json.loads(row["payload_json"])
            expected_hash = _hash({
                "idempotency_key": row["idempotency_key"],
                "event_type": row["event_type"], "payload": payload,
                "previous_event_hash": previous,
            })
            if expected_hash != row["event_hash"]:
                return {"schema": SCHEMA, "status": "INVALID",
                        "reason_code": "event_tampered",
                        "verified_events": expected_sequence - 1}
            previous = row["event_hash"]
        return {"schema": SCHEMA, "status": "VERIFIED",
                "reason_code": "ok", "verified_events": len(rows),
                "head_hash": previous, "offline": True}

    def status(self, key: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM hookwall_effects WHERE idempotency_key=?", (key,)
            ).fetchone()
        return dict(row) if row is not None else None


__all__ = ["HookwallEffectLedger", "SCHEMA"]
