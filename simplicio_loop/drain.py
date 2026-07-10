"""Composed queue-drain verification."""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence

SCHEMA = "simplicio.drain-receipt/v1"
ACTIVE_STATES = {"claimed", "running", "verification", "delivery"}


def _fail(code: str, detail: str, **extra: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "schema": SCHEMA,
        "verdict": "CONTINUE",
        "ready": False,
        "reason_code": code,
        "reason": detail,
        "tag": "UNVERIFIED",
    }
    result.update(extra)
    return result


def _same_polls(polls: Sequence[Any], required: int) -> bool:
    if required < 1 or len(polls) < required:
        return False
    tail = list(polls[-required:])
    return all(item == tail[0] for item in tail[1:])


def _task_id(task: Mapping[str, Any], index: int) -> str:
    return str(task.get("id") or "T%d" % (index + 1))


def evaluate_drain(snapshot: Mapping[str, Any], polls_required: int = 2) -> Dict[str, Any]:
    """Recompute a queue verdict from an immutable scheduler/source snapshot.

    ``snapshot`` contains ``tasks``, ``active_leases`` and chronological source ``polls``.
    A task is complete only when its watcher and oracle evidence is fresh and measured and
    its delivery target is satisfied.
    """
    if not isinstance(snapshot, Mapping):
        return _fail("snapshot_invalid", "drain snapshot is not an object")
    tasks = snapshot.get("tasks")
    if not isinstance(tasks, list):
        return _fail("tasks_missing", "drain snapshot has no task list")
    if not _same_polls(snapshot.get("polls") or [], polls_required):
        return _fail(
            "source_not_quiet",
            "source has not returned the same empty snapshot for the required polls",
            polls_required=polls_required,
        )

    active_leases = snapshot.get("active_leases", 0)
    if not isinstance(active_leases, int) or active_leases < 0:
        return _fail("leases_invalid", "active_leases must be a non-negative integer")
    if active_leases:
        return _fail("leases_active", "active leases remain", active_leases=active_leases)

    pending: List[str] = []
    evidence_pending: List[str] = []
    for index, raw_task in enumerate(tasks):
        if not isinstance(raw_task, Mapping):
            return _fail("task_invalid", "task record is not an object", task_index=index)
        task_id = _task_id(raw_task, index)
        state = str(raw_task.get("state") or "").strip().lower()
        if state in ACTIVE_STATES or state in {"ready", "blocked", "dead-letter"}:
            pending.append(task_id)
            continue
        if state != "done":
            return _fail("task_state_unknown", "task has no terminal verified state", task_id=task_id, state=state)
        evidence = raw_task.get("evidence")
        if not isinstance(evidence, Mapping):
            evidence_pending.append(task_id)
            continue
        if evidence.get("watcher_status") != "MEASURED" or not evidence.get("watcher_match"):
            evidence_pending.append(task_id)
            continue
        if evidence.get("oracle_verdict") not in {"COMPLETE", "DRAINED"}:
            evidence_pending.append(task_id)
            continue
        if not bool(raw_task.get("delivery_satisfied", False)):
            evidence_pending.append(task_id)

    if pending:
        return _fail("tasks_pending", "queue still has unresolved tasks", pending_tasks=pending)
    if evidence_pending:
        return _fail("evidence_pending", "done tasks lack fresh measured evidence", evidence_pending=evidence_pending)

    return {
        "schema": SCHEMA,
        "verdict": "DRAINED",
        "ready": True,
        "reason_code": "drain_verified",
        "reason": "source quiet, no active leases, all tasks verified and delivered",
        "tag": "MEASURED",
        "polls_required": polls_required,
        "polls_observed": len(snapshot.get("polls") or []),
        "task_count": len(tasks),
        "active_leases": 0,
    }


__all__ = ["SCHEMA", "evaluate_drain"]
