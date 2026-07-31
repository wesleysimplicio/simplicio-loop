"""Versioned metadata policy shared by loop event producers and projections."""
from __future__ import annotations

from typing import Any, Mapping

SCHEMA = "simplicio.event-metadata/v1"
SCOPES = ("collection", "task", "scenario")

COLLECTION_EVENT_KINDS = frozenset({
    "contract_frozen", "watcher_challenge", "phase_transition",
    "mapper_fresh", "mapper_degraded", "plan_ready", "intake",
    "mapping", "planning", "executing", "validating", "watching",
    "delivering", "done", "blocked", "cancelled", "awaiting_decision",
})
TASK_EVENT_KINDS = frozenset({
    "worker_claimed", "worktree_created", "operator_receipt", "test_gate",
    "operator_bootstrap", "delivery_reconciled", "rollback", "handoff",
    "technical_debt",
})
SCENARIO_EVENT_KINDS = frozenset({
    "scenario", "scenario_started", "scenario_completed", "scenario_failed",
})

POLICY: dict[str, Any] = {
    "schema": SCHEMA,
    "scope_field": "scope",
    "scopes": {
        "collection": {"task_id": "optional-null", "ac_ids": "optional"},
        "task": {"task_id": "required", "ac_ids": "optional"},
        "scenario": {"task_id": "required", "ac_ids": "required"},
    },
    "historical_compatibility": {
        "missing_scope": "infer-from-kind",
        "append_only": True,
        "legacy_schema": "simplicio.event-metadata/legacy",
    },
}

def _kind(event: Mapping[str, Any]) -> str:
    return str(event.get("kind") or event.get("event") or event.get("phase") or "unknown").strip().lower().replace("-", "_") or "unknown"


def infer_scope(event: Mapping[str, Any]) -> str:
    explicit = str(event.get("scope") or "").strip().lower()
    if explicit in SCOPES:
        return explicit
    if explicit:
        return "unknown"
    kind = _kind(event)
    if kind in COLLECTION_EVENT_KINDS:
        return "collection"
    if kind in SCENARIO_EVENT_KINDS or event.get("scenario_id"):
        return "scenario"
    if kind in TASK_EVENT_KINDS or event.get("task_id") or event.get("work_item_id"):
        return "task"
    if event.get("ac_ids") or event.get("ac_id"):
        return "scenario"
    return "unknown"


def _task_id(event: Mapping[str, Any], scope: str) -> str | None:
    if scope == "collection":
        return None
    value = event.get("task_id") or event.get("work_item_id")
    return str(value).strip() if value is not None and str(value).strip() else None


def validate_event_metadata(event: Mapping[str, Any], *, run_id: str = "") -> list[str]:
    scope = infer_scope(event)
    kind = _kind(event)
    event_id = str(event.get("event_id") or "legacy")
    diagnostics: list[str] = []
    if scope == "unknown":
        diagnostics.append(f"missing_event_metadata:event_id={event_id},kind={kind},scope=unknown")
    if not (str(event.get("run_id") or run_id).strip()):
        diagnostics.append(f"missing_event_metadata:event_id={event_id},kind={kind},scope={scope}:run_id")
    if scope in {"task", "scenario"} and _task_id(event, scope) is None:
        diagnostics.append(f"missing_event_metadata:event_id={event_id},kind={kind},scope={scope}:task_id")
    if scope == "scenario" and not (event.get("ac_ids") or event.get("ac_id") or event.get("scenario_id")):
        diagnostics.append(f"missing_event_metadata:event_id={event_id},kind={kind},scope=scenario:ac_id")
    if not (str(event.get("receipt") or event.get("receipt_ref") or "").strip() or str(event.get("blocker") or event.get("reason") or "").strip()):
        diagnostics.append(f"missing_event_metadata:event_id={event_id},kind={kind},scope={scope}:receipt_or_blocker")
    return diagnostics


def normalise_event(event: Mapping[str, Any], *, run_id: str = "", ac_ids: list[str] | None = None) -> dict[str, Any]:
    scope = infer_scope(event)
    kind = _kind(event)
    event_run_id = str(event.get("run_id") or run_id)
    event_task_id = _task_id(event, scope)
    event_ac_ids = ac_ids if ac_ids is not None else event.get("ac_ids", event.get("ac_id", ()))
    if isinstance(event_ac_ids, str):
        event_ac_ids = [event_ac_ids] if event_ac_ids.strip() else []
    elif not isinstance(event_ac_ids, (list, tuple, set)):
        event_ac_ids = []
    else:
        event_ac_ids = [str(value) for value in event_ac_ids if str(value).strip()]
    diagnostics = validate_event_metadata({**event, "scope": scope, "task_id": event_task_id, "ac_ids": event_ac_ids}, run_id=run_id)
    receipt = str(event.get("receipt") or event.get("receipt_ref") or "")
    blocker = str(event.get("blocker") or event.get("reason") or "")
    if diagnostics and not blocker:
        blocker = ";".join(diagnostics)
    return {
        "schema": str(event.get("schema") or SCHEMA),
        "source_schema": str(event.get("schema") or POLICY["historical_compatibility"]["legacy_schema"]),
        "event_id": str(event.get("event_id") or ""),
        "kind": kind,
        "phase": str(event.get("phase") or kind),
        "scope": scope,
        "status": str(event.get("status") or "INFO").upper(),
        "run_id": event_run_id,
        "task_id": event_task_id,
        "ac_ids": event_ac_ids,
        "receipt": receipt,
        "blocker": blocker,
        "metadata_status": "UNVERIFIED" if diagnostics else "MEASURED",
        "metadata_diagnostics": diagnostics,
        "message": str(event.get("message") or event.get("reason_code") or ""),
    }


def metadata_policy() -> dict[str, Any]:
    return {
        "schema": POLICY["schema"],
        "scope_field": POLICY["scope_field"],
        "scopes": {name: dict(values) for name, values in POLICY["scopes"].items()},
        "historical_compatibility": dict(POLICY["historical_compatibility"]),
    }
