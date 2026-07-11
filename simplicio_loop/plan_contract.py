"""Validation for mapper-derived plans.

The planner is deliberately a small, model-free boundary: a plan may only name
files inside the authorized repository, must carry the mapper pack and repository
identity it was derived from, and must account for every scenario and business
rule in the task contract.  This keeps stale or hand-written plans from silently
becoming operator authority.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

PLAN_SCHEMA = "simplicio.plan/v1"


def _state_matches(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    # Non-git repositories have no HEAD; their content hash is still a valid
    # local freshness identity.  A missing identity is handled by the caller.
    return bool(left.get("tree_hash")) and bool(right.get("tree_hash")) and (
        left.get("head") == right.get("head")
        and left.get("tree_hash") == right.get("tree_hash")
    )


def validate_plan(
    plan: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
    repo_path: str | Path,
    *,
    contract_hash: str = "",
    current_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic validation receipt for one mapper-derived plan."""
    errors: list[str] = []
    warnings: list[str] = []
    if plan.get("schema") != PLAN_SCHEMA:
        errors.append("plan_schema_invalid")
    if contract_hash and plan.get("task_contract_hash") != contract_hash:
        errors.append("task_contract_hash_mismatch")
    strict_identity = bool(str(plan.get("mapper_pack_hash") or "").strip() or (plan.get("repo_state") or {}).get("head"))
    if strict_identity and not str(plan.get("mapper_pack_hash") or "").strip():
        errors.append("mapper_pack_hash_missing")
    repo = Path(repo_path).resolve()
    state = plan.get("repo_state") or {}
    if not state.get("tree_hash"):
        errors.append("repo_state_missing")
    freshness = plan.get("freshness") or {}
    if freshness.get("verified") is not True:
        errors.append("mapper_freshness_unverified")
    observed = current_state or freshness.get("current_state") or {}
    if state.get("tree_hash") and observed.get("tree_hash") and not _state_matches(state, observed):
        errors.append("plan_repo_state_stale")

    steps = list(plan.get("steps") or [])
    if len(steps) != len(tasks):
        errors.append("task_step_count_mismatch")
    for index, task in enumerate(tasks, start=1):
        step = steps[index - 1] if index <= len(steps) and isinstance(steps[index - 1], Mapping) else {}
        expected_scenarios = {str(s.get("id")) for s in task.get("scenarios") or [] if s.get("id")}
        expected_rules = {str(r.get("id")) for r in task.get("rules") or [] if r.get("id")}
        actual_scenarios = {
            str(s.get("scenario_id") or s.get("id"))
            for s in step.get("steps") or []
            if s.get("scenario_id") or s.get("id")
        }
        actual_rules = {str(r) for r in step.get("rule_ids") or [] if str(r).strip()}
        for missing in sorted(expected_scenarios - actual_scenarios):
            errors.append(f"task[{index}] scenario_unplanned:{missing}")
        for missing in sorted(expected_rules - actual_rules):
            errors.append(f"task[{index}] rule_unplanned:{missing}")
        targets = list(step.get("candidate_targets") or [])
        if not targets:
            errors.append(f"task[{index}] targets_missing")
        for raw in targets:
            value = str(raw).replace("\\", "/").strip()
            try:
                path = (repo / value).resolve()
                path.relative_to(repo)
            except (OSError, ValueError):
                errors.append(f"task[{index}] target_outside_repo:{value}")
                continue
            if not path.exists() and value not in set(step.get("to_create") or []):
                errors.append(f"task[{index}] target_missing_without_to_create:{value}")

    return {
        "schema": "simplicio.plan-validation/v1",
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "checked_tasks": len(tasks),
    }


__all__ = ["PLAN_SCHEMA", "validate_plan"]
