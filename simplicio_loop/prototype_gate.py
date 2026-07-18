"""Prototype-First contract owned by Loop (issue #568).

This module is the small semantic control-plane shared by Mapper and Dev CLI.  It
does not generate code or call a model; it freezes the plan, budget and decision
boundary so adapters cannot silently bypass the gate.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

PLAN_SCHEMA = "simplicio.prototype-plan/v1"
DECISION_SCHEMA = "simplicio.prototype-decision/v1"
TYPES = frozenset(("wireframe", "architecture_diagram", "schema", "data_model", "failing_reproducer", "benchmark_spike", "mock_or_fake", "code_spike", "vertical_slice", "prompt_candidate", "workflow_simulation", "storyboard", "policy_or_security_model"))
LEVELS = ("P0", "P1", "P2", "FULL")
DEFAULT_BUDGET = {"P0": 0.03, "P1": 0.10, "P2": 0.20, "FULL": 1.0}


class PrototypeGateError(ValueError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _without_hash(payload: Mapping[str, Any]) -> dict[str, Any]:
    field = "decision_hash" if "decision_hash" in payload else "plan_hash"
    return {key: value for key, value in payload.items() if key != field}


def build_plan(*, work_item_id: str, goal: str, prototype_type: str, source_sha: str,
               level: str = "P1", estimated_budget: int | float = 0,
               validators: list[str] | None = None, context_pack_hash: str = "",
               negative_space: list[str] | None = None) -> dict[str, Any]:
    """Build a hash-bound plan accepted by all downstream adapters."""
    if not str(work_item_id).strip() or not str(goal).strip() or not str(source_sha).strip():
        raise PrototypeGateError("work_item_id, goal and source_sha are required")
    if prototype_type not in TYPES:
        raise PrototypeGateError(f"unsupported prototype_type: {prototype_type}")
    if level not in LEVELS:
        raise PrototypeGateError(f"unsupported prototype level: {level}")
    if not isinstance(estimated_budget, (int, float)) or estimated_budget < 0:
        raise PrototypeGateError("estimated_budget must be non-negative")
    payload: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "work_item_id": str(work_item_id),
        "goal": str(goal).strip(),
        "prototype_type": prototype_type,
        "source_sha": str(source_sha),
        "level": level,
        "budget_fraction": DEFAULT_BUDGET[level],
        "estimated_budget": estimated_budget,
        "validators": list(validators or []),
        "context_pack_hash": str(context_pack_hash),
        "negative_space": sorted({str(path) for path in (negative_space or []) if str(path).strip()}),
    }
    payload["plan_hash"] = _hash(payload)
    return payload


def validate_plan(plan: Mapping[str, Any], *, current_source_sha: str | None = None) -> dict[str, Any]:
    """Validate schema, hash and optional source drift without mutating state."""
    if not isinstance(plan, Mapping) or plan.get("schema") != PLAN_SCHEMA:
        raise PrototypeGateError("unsupported prototype plan schema")
    if plan.get("plan_hash") != _hash(_without_hash(plan)):
        raise PrototypeGateError("prototype plan hash mismatch")
    if plan.get("prototype_type") not in TYPES or plan.get("level") not in LEVELS:
        raise PrototypeGateError("prototype type or level is invalid")
    drift = current_source_sha is not None and str(current_source_sha) != str(plan.get("source_sha"))
    result = dict(plan)
    result["source_drift"] = drift
    if drift:
        result["valid"] = False
        result["reason_code"] = "source_drift"
    else:
        result["valid"] = True
    return result


def build_decision(*, plan: Mapping[str, Any], candidate_hash: str, decision: str,
                   reason: str = "") -> dict[str, Any]:
    """Create an auditable ACCEPT/REVISE/REJECT/BLOCKED decision receipt."""
    validated = validate_plan(plan)
    if decision not in {"ACCEPT", "REVISE", "REJECT", "BLOCKED"}:
        raise PrototypeGateError("invalid prototype decision")
    payload: dict[str, Any] = {
        "schema": DECISION_SCHEMA,
        "plan_hash": validated["plan_hash"],
        "source_sha": validated["source_sha"],
        "candidate_hash": str(candidate_hash),
        "decision": decision,
        "reason": str(reason),
    }
    payload["decision_hash"] = _hash(payload)
    return payload


def validate_decision(decision: Mapping[str, Any], *, plan: Mapping[str, Any], candidate_hash: str,
                      current_source_sha: str | None = None) -> dict[str, Any]:
    """Fail closed on forged, stale, drifted or non-ACCEPT receipts."""
    plan_result = validate_plan(plan, current_source_sha=current_source_sha)
    if not plan_result["valid"]:
        raise PrototypeGateError("prototype decision source drift")
    if decision.get("schema") != DECISION_SCHEMA or decision.get("decision_hash") != _hash(_without_hash(decision)):
        raise PrototypeGateError("prototype decision schema/hash mismatch")
    if decision.get("plan_hash") != plan.get("plan_hash") or decision.get("candidate_hash") != candidate_hash:
        raise PrototypeGateError("prototype decision is stale or not bound to candidate")
    if current_source_sha is not None and decision.get("source_sha") != current_source_sha:
        raise PrototypeGateError("prototype decision source drift")
    if decision.get("decision") != "ACCEPT":
        raise PrototypeGateError(f"prototype decision is {decision.get('decision')!r}, not ACCEPT")
    return dict(decision)


__all__ = ["PLAN_SCHEMA", "DECISION_SCHEMA", "PrototypeGateError", "build_plan", "validate_plan", "build_decision", "validate_decision"]
