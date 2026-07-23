"""Hub-owned state for the external Code -> Loop worker protocol.

The store is deliberately a small durable boundary.  It owns workflow identity,
task leases, event cursors and cancellation authority; it never starts a worker,
selects a provider or performs workspace effects.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List


WORKER_SCHEMA = "simplicio.code-worker-adapter/v1"
WORKER_PROTOCOL = "simplicio.loop-worker/v1"
WORKER_STATES = {"waiting", "working", "blocked", "failed", "done", "cancelled"}
TERMINAL_STATES = {"failed", "done", "cancelled"}


class HubWorkerError(RuntimeError):
    """Invalid, stale or unavailable worker workflow request."""


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HubWorkerError(f"{name} must be a non-empty string")
    return value


def _validate_delegate(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    if payload.get("schema") != WORKER_SCHEMA or payload.get("protocol") != WORKER_PROTOCOL:
        raise HubWorkerError("unsupported worker schema or protocol")
    identity = payload.get("identity")
    if not isinstance(identity, dict):
        raise HubWorkerError("worker identity must be an object")
    for name in ("coordinator_id", "session_id", "turn_id", "run_id", "goal_id"):
        _require_text(identity.get(name), f"identity.{name}")
    key = _require_text(payload.get("idempotency_key"), "idempotency_key")
    if len(key) > 512:
        raise HubWorkerError("idempotency_key exceeds the 512 byte limit")
    try:
        max_concurrency = int(payload.get("max_concurrency", 0))
    except (TypeError, ValueError) as exc:
        raise HubWorkerError("max_concurrency must be a positive integer") from exc
    if max_concurrency < 1 or max_concurrency > 256:
        raise HubWorkerError("max_concurrency must be between 1 and 256")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise HubWorkerError("tasks must be a non-empty list")
    if len(tasks) > 4096:
        raise HubWorkerError("worker DAG exceeds the 4096 task limit")
    ids = set()
    normalized: List[Dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            raise HubWorkerError("each worker task must be an object")
        task_id = _require_text(task.get("task_id"), "task.task_id")
        role = _require_text(task.get("role"), "task.role")
        if role not in {"implementer", "reviewer", "tester", "delivery"}:
            raise HubWorkerError(f"unsupported worker role: {role}")
        contract = _require_text(task.get("task_contract"), "task.task_contract")
        dependencies = task.get("depends_on", [])
        if not isinstance(dependencies, list) or any(not isinstance(dep, str) for dep in dependencies):
            raise HubWorkerError(f"task {task_id} dependencies must be a string list")
        if task_id in ids:
            raise HubWorkerError("task IDs must be unique")
        ids.add(task_id)
        normalized.append({
            "task_id": task_id,
            "role": role,
            "depends_on": list(dependencies),
            "task_contract": contract,
        })
    for task in normalized:
        if task["task_id"] in task["depends_on"] or any(dep not in ids for dep in task["depends_on"]):
            raise HubWorkerError(f"task {task['task_id']} has a missing or self dependency")
    graph = {task["task_id"]: task["depends_on"] for task in normalized}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            raise HubWorkerError("task DAG contains a cycle")
        visiting.add(task_id)
        for dependency in graph[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in graph:
        visit(task_id)
    return normalized


class HubWorkerStore:
    """Durable, Hub-owned reducer for worker delegation and cancellation."""

    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS worker_workflows (
                    workflow_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_digest TEXT NOT NULL,
                    identity_json TEXT NOT NULL,
                    max_concurrency INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    mutation_authority INTEGER NOT NULL,
                    delegate_receipt_id TEXT NOT NULL,
                    cancel_receipt_json TEXT,
                    created REAL NOT NULL,
                    updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS worker_tasks (
                    workflow_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    depends_on_json TEXT NOT NULL,
                    task_contract TEXT NOT NULL,
                    state TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    fence INTEGER NOT NULL,
                    worktree_id TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    path_token TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    reason TEXT,
                    receipt_id TEXT,
                    PRIMARY KEY(workflow_id, task_id)
                );
                CREATE TABLE IF NOT EXISTS worker_events (
                    workflow_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY(workflow_id, sequence)
                );
                """
            )

    def close(self) -> None:
        self._db.close()

    def _workflow(self, workflow_id: str) -> sqlite3.Row:
        row = self._db.execute(
            "SELECT * FROM worker_workflows WHERE workflow_id=?", (workflow_id,)
        ).fetchone()
        if row is None:
            raise HubWorkerError("unknown worker workflow")
        return row

    def _receipt(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "schema": WORKER_SCHEMA,
            "workflow_id": row["workflow_id"],
            "receipt_id": row["delegate_receipt_id"],
            "accepted_task_ids": [
                item["task_id"] for item in self._db.execute(
                    "SELECT task_id FROM worker_tasks WHERE workflow_id=? ORDER BY rowid",
                    (row["workflow_id"],),
                ).fetchall()
            ],
        }

    def _append_event(self, workflow_id: str, task: sqlite3.Row, *, state: str,
                      reason: Any = None, receipt_id: Any = None) -> None:
        last = self._db.execute(
            "SELECT COALESCE(MAX(sequence), -1) AS sequence FROM worker_events WHERE workflow_id=?",
            (workflow_id,),
        ).fetchone()["sequence"]
        sequence = int(last) + 1
        event_id = f"worker-event:{workflow_id}:{sequence}"
        event = {
            "sequence": sequence,
            "event_id": event_id,
            "causal_event_id": None,
            "task_id": task["task_id"],
            "role": task["role"],
            "attempt": {
                "stage_id": f"worker-stage:{task['task_id']}",
                "agent_id": task["owner"],
                "worktree_id": task["worktree_id"],
                "attempt": task["attempt"],
                "fence": task["fence"],
            },
            "state": state,
            "owner": "loop-hub",
            "reason": reason,
            "lease": {
                "worktree_id": task["worktree_id"],
                "branch": task["branch"],
                "path_token": task["path_token"],
                "lease_id": task["lease_id"],
                "fence": task["fence"],
            },
            "receipt_id": receipt_id,
        }
        self._db.execute(
            "INSERT INTO worker_events(workflow_id,sequence,event_json) VALUES (?,?,?)",
            (workflow_id, sequence, json.dumps(event, sort_keys=True)),
        )

    def delegate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        tasks = _validate_delegate(payload)
        identity = payload["identity"]
        key = payload["idempotency_key"]
        normalized = {
            "schema": payload["schema"], "protocol": payload["protocol"],
            "identity": identity, "idempotency_key": key,
            "max_concurrency": int(payload["max_concurrency"]), "tasks": tasks,
        }
        request_digest = _digest(normalized)
        with self._lock, self._db:
            existing = self._db.execute(
                "SELECT * FROM worker_workflows WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise HubWorkerError("conflicting worker idempotency key reuse")
                return self._receipt(existing)
            workflow_id = "worker:" + _digest(key)[:32]
            receipt_id = "delegate:" + _digest(key)[:32]
            now = time.time()
            self._db.execute(
                "INSERT INTO worker_workflows VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (workflow_id, key, request_digest, json.dumps(identity, sort_keys=True),
                 int(payload["max_concurrency"]), "running", 1, receipt_id, None, now, now),
            )
            for index, task in enumerate(tasks, start=1):
                task_id = task["task_id"]
                worktree_id = f"worker:{workflow_id}:{task_id}"
                path_token = _digest([workflow_id, task_id])
                self._db.execute(
                    "INSERT INTO worker_tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (workflow_id, task_id, task["role"], json.dumps(task["depends_on"]),
                     task["task_contract"], "waiting", f"external-agent:{task_id}", 1, index,
                     worktree_id, f"worker/{workflow_id}/{task_id}", path_token,
                     f"lease:{workflow_id}:{task_id}:1", None, None),
                )
                row = self._db.execute(
                    "SELECT * FROM worker_tasks WHERE workflow_id=? AND task_id=?",
                    (workflow_id, task_id),
                ).fetchone()
                self._append_event(workflow_id, row, state="waiting")
            return self._receipt(self._workflow(workflow_id))

    def status(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        workflow_id = _require_text(payload.get("workflow_id"), "workflow_id")
        try:
            after = int(payload.get("after_sequence", 0))
        except (TypeError, ValueError) as exc:
            raise HubWorkerError("after_sequence must be a non-negative integer") from exc
        if after < 0:
            raise HubWorkerError("after_sequence must be non-negative")
        with self._lock:
            self._workflow(workflow_id)
            rows = self._db.execute(
                "SELECT event_json FROM worker_events WHERE workflow_id=? AND sequence>=? ORDER BY sequence",
                (workflow_id, after),
            ).fetchall()
            next_sequence = self._db.execute(
                "SELECT COALESCE(MAX(sequence), -1) + 1 AS next_sequence FROM worker_events WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()["next_sequence"]
            return {
                "schema": WORKER_SCHEMA,
                "workflow_id": workflow_id,
                "next_sequence": int(next_sequence),
                "events": [json.loads(row["event_json"]) for row in rows],
            }

    def cancel(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        workflow_id = _require_text(payload.get("workflow_id"), "workflow_id")
        key = _require_text(payload.get("idempotency_key"), "idempotency_key")
        reason = _require_text(payload.get("reason"), "reason")
        if payload.get("revoke_mutation_authority") is not True:
            raise HubWorkerError("worker cancellation must revoke mutation authority")
        with self._lock, self._db:
            workflow = self._workflow(workflow_id)
            if workflow["cancel_receipt_json"] is not None:
                return json.loads(workflow["cancel_receipt_json"])
            receipt = {
                "schema": WORKER_SCHEMA,
                "workflow_id": workflow_id,
                "receipt_id": "cancel:" + _digest(key)[:32],
                "accepted_task_ids": [],
            }
            self._db.execute(
                "UPDATE worker_workflows SET state='cancelled',mutation_authority=0,cancel_receipt_json=?,updated=? WHERE workflow_id=?",
                (json.dumps(receipt, sort_keys=True), time.time(), workflow_id),
            )
            tasks = self._db.execute(
                "SELECT * FROM worker_tasks WHERE workflow_id=? AND state NOT IN ('failed','done','cancelled') ORDER BY rowid",
                (workflow_id,),
            ).fetchall()
            for task in tasks:
                self._db.execute(
                    "UPDATE worker_tasks SET state='cancelled',reason=?,receipt_id=? WHERE workflow_id=? AND task_id=?",
                    (reason, receipt["receipt_id"], workflow_id, task["task_id"]),
                )
                updated = self._db.execute(
                    "SELECT * FROM worker_tasks WHERE workflow_id=? AND task_id=?",
                    (workflow_id, task["task_id"]),
                ).fetchone()
                self._append_event(workflow_id, updated, state="cancelled", reason=reason,
                                   receipt_id=receipt["receipt_id"])
            return receipt

    def deliver(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        workflow_id = _require_text(payload.get("workflow_id"), "workflow_id")
        task_id = _require_text(payload.get("task_id"), "task_id")
        _require_text(payload.get("agent_id"), "agent_id")
        _require_text(payload.get("review_receipt_id"), "review_receipt_id")
        with self._lock:
            workflow = self._workflow(workflow_id)
            task = self._db.execute(
                "SELECT * FROM worker_tasks WHERE workflow_id=? AND task_id=?", (workflow_id, task_id)
            ).fetchone()
            if task is None:
                raise HubWorkerError("unknown worker task")
            if workflow["mutation_authority"] != 1:
                raise HubWorkerError("worker mutation authority was revoked")
            if task["role"] != "delivery" or task["state"] != "done":
                raise HubWorkerError("delivery requires a done delivery-role task")
            # A Hub receipt is intentionally not a remote PR confirmation.  Code's
            # client rejects this value, keeping the goal->PR gate fail-closed until
            # an authenticated external publisher supplies the real confirmation.
            return {
                "schema": WORKER_SCHEMA,
                "workflow_id": workflow_id,
                "receipt_id": "delivery:" + _digest(payload)[:32],
                "remote_reference": f"unconfirmed:{workflow_id}/{task_id}",
                "remotely_confirmed": False,
            }
