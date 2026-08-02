"""Hub-owned state for the external Code -> Loop worker protocol.

The store is deliberately a small durable boundary.  It owns workflow identity,
task leases, event cursors and cancellation authority; it never starts a worker,
selects a provider or performs workspace effects.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping

from .mapper_operations import MapperOperationsAdapter


WORKER_SCHEMA = "simplicio.code-worker-adapter/v1"
WORKER_PROTOCOL = "simplicio.loop-worker/v1"
WORKER_STATES = {"waiting", "working", "blocked", "failed", "done", "cancelled"}
TERMINAL_STATES = {"failed", "done", "cancelled"}
EVENT_SCHEMA = "simplicio.loop-worker-store-event/v1"
JOURNAL_PREFIX = "simplicio.loop.hub-worker:"


class HubWorkerError(RuntimeError):
    """Invalid, stale or unavailable worker workflow request."""


class _JournalConflict(HubWorkerError):
    """The projected state changed before this mutation committed."""


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
    """Durable, Hub-owned reducer projected from the Mapper operations journal."""

    def __init__(self, path: str, *, operations: Any | None = None) -> None:
        self.path = str(Path(path).expanduser().absolute())
        self._operations = operations or MapperOperationsAdapter(self.path)
        self._journal_id = JOURNAL_PREFIX + self.path
        self._lock = threading.RLock()
        self._operations.initialize()

    def close(self) -> None:
        """Retain the old lifecycle hook; Mapper owns the underlying store."""

    @staticmethod
    def _last_seq(replay: Mapping[str, Any]) -> int:
        events = replay.get("events") or []
        if events:
            return int(events[-1]["seq"])
        compaction = replay.get("compaction")
        return int(compaction["through_seq"]) if compaction else 0

    def _replay(self) -> dict[str, Any]:
        replay = self._operations.replay(self._journal_id)
        if not replay.get("valid", False):
            raise HubWorkerError("worker journal is invalid")
        return replay

    @staticmethod
    def _state(replay: Mapping[str, Any]) -> dict[str, Any]:
        state: dict[str, Any] = {"workflows": {}, "tasks": {}, "events": {}}
        for journal_event in replay.get("events", []):
            payload = journal_event.get("payload")
            if not isinstance(payload, Mapping) or payload.get("schema") != EVENT_SCHEMA:
                raise HubWorkerError("worker journal contains an unknown event")
            operation = payload.get("operation")
            workflow_id = str(payload.get("workflow_id", ""))
            if operation == "workflow_created":
                workflow = dict(payload["workflow"])
                tasks = {str(task["task_id"]): dict(task) for task in payload["tasks"]}
                state["workflows"][workflow_id] = workflow
                state["tasks"][workflow_id] = tasks
                state["events"][workflow_id] = [dict(event) for event in payload["events"]]
            elif operation == "workflow_cancelled":
                state["workflows"][workflow_id] = dict(payload["workflow"])
                state["tasks"].setdefault(workflow_id, {}).update(
                    {str(task["task_id"]): dict(task) for task in payload["tasks"]}
                )
                state["events"].setdefault(workflow_id, []).extend(
                    dict(event) for event in payload["events"]
                )
            elif operation == "task_updated":
                state["tasks"].setdefault(workflow_id, {})[str(payload["task"]["task_id"])] = dict(
                    payload["task"]
                )
                state["events"].setdefault(workflow_id, []).extend(
                    dict(event) for event in payload.get("events", [])
                )
            else:
                raise HubWorkerError("worker journal contains an unknown operation")
        return state

    @staticmethod
    def _is_conflict(error: BaseException) -> bool:
        return "JOURNAL_CONFLICT" in str(error) or getattr(error, "reason_code", "") == "JOURNAL_CONFLICT"

    def _append(self, replay: Mapping[str, Any], event_type: str, payload: Mapping[str, Any]) -> None:
        try:
            self._operations.append_event(
                self._journal_id,
                event_type,
                {"schema": EVENT_SCHEMA, **dict(payload)},
                expected_seq=self._last_seq(replay),
            )
        except Exception as error:
            if self._is_conflict(error):
                raise _JournalConflict("worker journal changed concurrently") from error
            raise HubWorkerError("worker journal append failed: " + str(error)) from error

    @staticmethod
    def _workflow(state: Mapping[str, Any], workflow_id: str) -> dict[str, Any]:
        workflow = state["workflows"].get(workflow_id)
        if workflow is None:
            raise HubWorkerError("unknown worker workflow")
        return workflow

    @staticmethod
    def _receipt(state: Mapping[str, Any], workflow: Mapping[str, Any]) -> Dict[str, Any]:
        workflow_id = str(workflow["workflow_id"])
        return {
            "schema": WORKER_SCHEMA,
            "workflow_id": workflow_id,
            "receipt_id": workflow["delegate_receipt_id"],
            "accepted_task_ids": [
                task["task_id"] for task in state["tasks"][workflow_id].values()
            ],
        }

    @staticmethod
    def _event(workflow_id: str, task: Mapping[str, Any], sequence: int, *, state: str,
               reason: Any = None, receipt_id: Any = None) -> dict[str, Any]:
        event_id = f"worker-event:{workflow_id}:{sequence}"
        return {
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
        workflow_id = "worker:" + _digest(key)[:32]
        receipt_id = "delegate:" + _digest(key)[:32]
        with self._lock:
            for _ in range(32):
                replay = self._replay()
                state = self._state(replay)
                existing = next(
                    (workflow for workflow in state["workflows"].values()
                     if workflow["idempotency_key"] == key),
                    None,
                )
                if existing is not None:
                    if existing["request_digest"] != request_digest:
                        raise HubWorkerError("conflicting worker idempotency key reuse")
                    return self._receipt(state, existing)
                now = time.time()
                workflow = {
                    "workflow_id": workflow_id,
                    "idempotency_key": key,
                    "request_digest": request_digest,
                    "identity": identity,
                    "max_concurrency": int(payload["max_concurrency"]),
                    "state": "running",
                    "mutation_authority": 1,
                    "delegate_receipt_id": receipt_id,
                    "cancel_receipt": None,
                    "created": now,
                    "updated": now,
                }
                task_rows = []
                events = []
                for index, task in enumerate(tasks, start=1):
                    task_id = task["task_id"]
                    row = {
                        "workflow_id": workflow_id,
                        "task_id": task_id,
                        "role": task["role"],
                        "depends_on": list(task["depends_on"]),
                        "task_contract": task["task_contract"],
                        "state": "waiting",
                        "owner": f"external-agent:{task_id}",
                        "attempt": 1,
                        "fence": index,
                        "worktree_id": f"worker:{workflow_id}:{task_id}",
                        "branch": f"worker/{workflow_id}/{task_id}",
                        "path_token": _digest([workflow_id, task_id]),
                        "lease_id": f"lease:{workflow_id}:{task_id}:1",
                        "reason": None,
                        "receipt_id": None,
                    }
                    task_rows.append(row)
                    events.append(self._event(workflow_id, row, index - 1, state="waiting"))
                try:
                    self._append(replay, "worker.workflow-created", {
                        "operation": "workflow_created", "workflow_id": workflow_id,
                        "workflow": workflow, "tasks": task_rows, "events": events,
                    })
                    return self._receipt(
                        {"workflows": {workflow_id: workflow}, "tasks": {workflow_id: {
                            task["task_id"]: task for task in task_rows
                        }}},
                        workflow,
                    )
                except _JournalConflict:
                    continue
        raise HubWorkerError("worker delegation remained contended")

    def status(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        workflow_id = _require_text(payload.get("workflow_id"), "workflow_id")
        try:
            after = int(payload.get("after_sequence", 0))
        except (TypeError, ValueError) as exc:
            raise HubWorkerError("after_sequence must be a non-negative integer") from exc
        if after < 0:
            raise HubWorkerError("after_sequence must be non-negative")
        with self._lock:
            state = self._state(self._replay())
            self._workflow(state, workflow_id)
            events = state["events"].get(workflow_id, [])
            return {
                "schema": WORKER_SCHEMA,
                "workflow_id": workflow_id,
                "next_sequence": len(events),
                "events": events[after:],
            }

    def cancel(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        workflow_id = _require_text(payload.get("workflow_id"), "workflow_id")
        key = _require_text(payload.get("idempotency_key"), "idempotency_key")
        reason = _require_text(payload.get("reason"), "reason")
        if payload.get("revoke_mutation_authority") is not True:
            raise HubWorkerError("worker cancellation must revoke mutation authority")
        with self._lock:
            for _ in range(32):
                replay = self._replay()
                state = self._state(replay)
                workflow = self._workflow(state, workflow_id)
                if workflow["cancel_receipt"] is not None:
                    return dict(workflow["cancel_receipt"])
                receipt = {
                    "schema": WORKER_SCHEMA,
                    "workflow_id": workflow_id,
                    "receipt_id": "cancel:" + _digest(key)[:32],
                    "accepted_task_ids": [],
                }
                updated_workflow = {**workflow, "state": "cancelled", "mutation_authority": 0,
                                    "cancel_receipt": receipt, "updated": time.time()}
                updated_tasks = []
                events = []
                sequence = len(state["events"].get(workflow_id, []))
                for task in state["tasks"][workflow_id].values():
                    if task["state"] in TERMINAL_STATES:
                        continue
                    updated = {**task, "state": "cancelled", "reason": reason,
                                "receipt_id": receipt["receipt_id"]}
                    updated_tasks.append(updated)
                    events.append(self._event(workflow_id, updated, sequence, state="cancelled",
                                              reason=reason, receipt_id=receipt["receipt_id"]))
                    sequence += 1
                try:
                    self._append(replay, "worker.workflow-cancelled", {
                        "operation": "workflow_cancelled", "workflow_id": workflow_id,
                        "workflow": updated_workflow, "tasks": updated_tasks, "events": events,
                    })
                    return receipt
                except _JournalConflict:
                    continue
        raise HubWorkerError("worker cancellation remained contended")

    def deliver(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        workflow_id = _require_text(payload.get("workflow_id"), "workflow_id")
        task_id = _require_text(payload.get("task_id"), "task_id")
        _require_text(payload.get("agent_id"), "agent_id")
        _require_text(payload.get("review_receipt_id"), "review_receipt_id")
        with self._lock:
            state = self._state(self._replay())
            workflow = self._workflow(state, workflow_id)
            task = state["tasks"].get(workflow_id, {}).get(task_id)
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
