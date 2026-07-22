"""Durable, fenced state machine for Hub stage-agent executions.

This module persists intent only.  It deliberately has no process-launching or
``admitted_held`` activation path.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence, Tuple

JOB_SCHEMA = "contracts/hub-agent/v1/job"
HANDLE_SCHEMA = "contracts/hub-agent/v1/handle"
RECEIPT_SCHEMA = "contracts/hub-agent/v1/execution-receipt"
STORE_SCHEMA_VERSION = 1
TERMINAL_STATES = frozenset(("succeeded", "failed", "cancelled", "timed_out"))
STATES = frozenset(("prepared", "queued", "leased", "running", "recovery_unknown")) | TERMINAL_STATES
_TRANSITIONS = {
    "prepared": frozenset(("queued",)),
    "queued": frozenset(("leased",)),
    "leased": frozenset(("running", "queued", "recovery_unknown")),
    "running": frozenset(TERMINAL_STATES | frozenset(("recovery_unknown",))),
    "recovery_unknown": frozenset(("queued",) + tuple(TERMINAL_STATES)),
}
_REQUIRED_IDS = ("graph_id", "run_id", "task_id", "stage_id", "role", "attempt_id")


class HubAgentStoreError(RuntimeError):
    reason_code = "hub_agent_store_error"


class ValidationError(HubAgentStoreError):
    reason_code = "invalid_record"


class IdempotencyConflict(HubAgentStoreError):
    reason_code = "idempotency_conflict"


class TransitionConflict(HubAgentStoreError):
    reason_code = "transition_conflict"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 4096:
        raise ValidationError("%s must be non-empty bounded text" % name)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValidationError("%s contains control characters" % name)
    return value


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValidationError("%s must be a lowercase sha256" % name)
    return value


def _validate_process_spec(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError("process_spec must be an object")
    spec = dict(value)
    argv = spec.get("argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x for x in argv):
        raise ValidationError("process_spec.argv must be non-empty strings")
    if spec.get("shell", False) is not False:
        raise ValidationError("shell process specs are forbidden")
    return spec


def build_job(*, idempotency_key: str, source_fence: str, plan_revision: str,
              input_hash: str, context_hash: str, process_spec: Mapping[str, Any],
              deadline: str, priority: int, resources: Mapping[str, Any], **identifiers: str) -> Dict[str, Any]:
    job = {"schema": JOB_SCHEMA, "idempotency_key": _text(idempotency_key, "idempotency_key"),
           "source_fence": _text(source_fence, "source_fence"),
           "plan_revision": _text(plan_revision, "plan_revision"),
           "input_hash": _hash(input_hash, "input_hash"), "context_hash": _hash(context_hash, "context_hash"),
           "process_spec": _validate_process_spec(process_spec), "deadline": _text(deadline, "deadline"),
           "priority": priority, "resources": dict(resources)}
    for name in _REQUIRED_IDS:
        job[name] = _text(identifiers.get(name), name)
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise ValidationError("priority must be an integer")
    if not isinstance(resources, Mapping):
        raise ValidationError("resources must be an object")
    job["content_hash"] = _digest(job)
    return job


def validate_job(value: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError("job must be an object")
    raw = dict(value)
    expected = raw.pop("content_hash", None)
    if raw.get("schema") != JOB_SCHEMA or expected != _digest(raw):
        raise ValidationError("job schema or content hash is invalid")
    rebuilt = build_job(**{k: v for k, v in raw.items() if k != "schema"})
    if rebuilt != dict(value):
        raise ValidationError("job content is invalid")
    return rebuilt


def validate_receipt(value: Mapping[str, Any], *, job_id: str, generation: int, fence: str,
                     terminal_state: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError("receipt must be an object")
    receipt = dict(value)
    expected = receipt.pop("receipt_hash", None)
    if (receipt.get("schema") != RECEIPT_SCHEMA or receipt.get("job_id") != job_id
            or receipt.get("generation") != generation or receipt.get("fence") != fence
            or receipt.get("terminal_state") != terminal_state or expected != _digest(receipt)):
        raise ValidationError("receipt identity or hash is invalid")
    receipt["receipt_hash"] = expected
    return receipt


class HubAgentStore:
    """Transactional SQLite authority for stage-agent jobs and fencing."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS hub_agent_meta (version INTEGER NOT NULL)")
            row = db.execute("SELECT version FROM hub_agent_meta").fetchone()
            if row is None:
                db.execute("INSERT INTO hub_agent_meta VALUES (?)", (STORE_SCHEMA_VERSION,))
            elif row[0] != STORE_SCHEMA_VERSION:
                raise ValidationError("unsupported store schema version")
            db.execute("""CREATE TABLE IF NOT EXISTS hub_agent_jobs (
                job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, content_hash TEXT NOT NULL,
                job_json TEXT NOT NULL, state TEXT NOT NULL, generation INTEGER NOT NULL,
                fence TEXT NOT NULL, receipt_json TEXT, created_ns INTEGER NOT NULL, updated_ns INTEGER NOT NULL)""")

    def prepare(self, job: Mapping[str, Any]) -> Tuple[Dict[str, Any], bool]:
        job = validate_job(job)
        now = time.time_ns()
        with self._transaction() as db:
            row = db.execute("SELECT * FROM hub_agent_jobs WHERE idempotency_key=?", (job["idempotency_key"],)).fetchone()
            if row is not None:
                existing = self._decode(row)
                if existing["job"]["content_hash"] != job["content_hash"]:
                    raise IdempotencyConflict("idempotency key already binds different content")
                return existing, False
            job_id = uuid.uuid4().hex
            fence = uuid.uuid4().hex
            db.execute("INSERT INTO hub_agent_jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (job_id, job["idempotency_key"], job["content_hash"], _canonical(job),
                        "prepared", 1, fence, None, now, now))
            row = db.execute("SELECT * FROM hub_agent_jobs WHERE job_id=?", (job_id,)).fetchone()
            return self._decode(row), True

    def get(self, job_id: str) -> Dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM hub_agent_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._decode(row)

    def transition(self, job_id: str, *, expected_state: str, generation: int, fence: str,
                   target_state: str, receipt: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        if expected_state not in STATES or target_state not in _TRANSITIONS.get(expected_state, frozenset()):
            raise TransitionConflict("illegal state transition")
        terminal = target_state in TERMINAL_STATES
        if terminal != (receipt is not None):
            raise ValidationError("terminal transitions require exactly one receipt")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM hub_agent_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["state"] != expected_state or row["generation"] != generation or row["fence"] != fence:
                raise TransitionConflict("stale state, generation, or fence")
            receipt_json = None
            if receipt is not None:
                receipt_json = _canonical(validate_receipt(receipt, job_id=job_id, generation=generation,
                                                           fence=fence, terminal_state=target_state))
            changed = db.execute("UPDATE hub_agent_jobs SET state=?,receipt_json=?,updated_ns=? "
                                 "WHERE job_id=? AND state=? AND generation=? AND fence=?",
                                 (target_state, receipt_json, time.time_ns(), job_id, expected_state, generation, fence))
            if changed.rowcount != 1:
                raise TransitionConflict("conditional transition lost")
            return self._decode(db.execute("SELECT * FROM hub_agent_jobs WHERE job_id=?", (job_id,)).fetchone())

    def recover(self, job_id: str, *, expected_state: str, generation: int, fence: str) -> Dict[str, Any]:
        """Fence an ambiguous lease/run after a crash and make uncertainty durable."""
        if expected_state not in ("leased", "running"):
            raise TransitionConflict("only leased/running jobs can become recovery_unknown")
        with self._transaction() as db:
            row = db.execute("SELECT state,generation,fence FROM hub_agent_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if tuple(row) != (expected_state, generation, fence):
                raise TransitionConflict("stale recovery fence")
            new_generation, new_fence = generation + 1, uuid.uuid4().hex
            db.execute("UPDATE hub_agent_jobs SET state='recovery_unknown',generation=?,fence=?,updated_ns=? WHERE job_id=?",
                       (new_generation, new_fence, time.time_ns(), job_id))
            return self._decode(db.execute("SELECT * FROM hub_agent_jobs WHERE job_id=?", (job_id,)).fetchone())

    @staticmethod
    def _decode(row: sqlite3.Row) -> Dict[str, Any]:
        job = validate_job(json.loads(row["job_json"]))
        if job["content_hash"] != row["content_hash"] or row["state"] not in STATES or row["generation"] < 1:
            raise ValidationError("persisted job metadata is corrupt")
        handle = {"schema": HANDLE_SCHEMA, "job_id": row["job_id"], "generation": row["generation"],
                  "fence": row["fence"], "idempotency_key": row["idempotency_key"]}
        handle["handle_hash"] = _digest(handle)
        receipt = None
        if row["receipt_json"] is not None:
            receipt = validate_receipt(json.loads(row["receipt_json"]), job_id=row["job_id"],
                                       generation=row["generation"], fence=row["fence"], terminal_state=row["state"])
        if (row["state"] in TERMINAL_STATES) != (receipt is not None):
            raise ValidationError("terminal state and receipt are inconsistent")
        return {"job": job, "handle": handle, "state": row["state"], "receipt": receipt,
                "created_ns": row["created_ns"], "updated_ns": row["updated_ns"]}


def build_receipt(*, job_id: str, generation: int, fence: str, terminal_state: str,
                  outcome: Mapping[str, Any], evidence_hashes: Sequence[str]) -> Dict[str, Any]:
    if terminal_state not in TERMINAL_STATES:
        raise ValidationError("receipt state must be terminal")
    receipt = {"schema": RECEIPT_SCHEMA, "job_id": _text(job_id, "job_id"), "generation": generation,
               "fence": _text(fence, "fence"), "terminal_state": terminal_state,
               "outcome": dict(outcome), "evidence_hashes": [_hash(value, "evidence_hash") for value in evidence_hashes]}
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ValidationError("generation must be positive")
    receipt["receipt_hash"] = _digest(receipt)
    return receipt
