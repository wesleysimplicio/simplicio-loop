"""Validation for mapper-derived plans.

The planner is deliberately a small, model-free boundary: a plan may only name
files inside the authorized repository, must carry the mapper pack and repository
identity it was derived from, and must account for every scenario and business
rule in the task contract.  This keeps stale or hand-written plans from silently
becoming operator authority.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

PLAN_SCHEMA = "simplicio.plan/v1"


def _state_matches(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    # Non-git repositories have no HEAD; their content hash is still a valid
    # local freshness identity.  A missing identity is handled by the caller.
    return bool(left.get("tree_hash")) and bool(right.get("tree_hash")) and (
        left.get("head") == right.get("head")
        and left.get("tree_hash") == right.get("tree_hash")
    )


def _step_ancestors(step_id: str, depends_on: Mapping[str, Sequence[str]],
                    seen: set[str] | None = None) -> set[str]:
    """Transitive closure of ``depends_on`` for one step id (cycle-safe)."""
    seen = seen if seen is not None else set()
    for dep in depends_on.get(step_id, ()):  # already validated to be strings
        if dep not in seen:
            seen.add(dep)
            _step_ancestors(dep, depends_on, seen)
    return seen


def _validate_dag(plan: Mapping[str, Any], steps: Sequence[Mapping[str, Any]],
                  errors: list[str]) -> None:
    """#284 plan/v1 DAG + parallelizable-step field (compatible evolution, not v2).

    Optional and strictly additive: a plan that never sets ``dag`` is completely
    unaffected (this function is a no-op). When present, ``plan["dag"]`` may carry:

      * ``parallel_groups``: a list of step-id lists the planner asserts can run
        concurrently.
      * each step may declare ``depends_on`` (a list of predecessor step ids).

    A parallel group is rejected -- fail closed, same discipline as the rest of
    this validator -- if any two of its members have a (possibly transitive)
    dependency edge between them: that is not a genuinely parallelizable pair,
    it is a mislabeled sequential dependency.
    """
    dag = plan.get("dag") or {}
    if not dag:
        return
    step_ids = [str(step.get("id") or f"T{index + 1}") for index, step in enumerate(steps)]
    step_id_set = set(step_ids)
    depends_on: Dict[str, list[str]] = {}
    for index, step_id in enumerate(step_ids):
        raw_deps = steps[index].get("depends_on") or [] if index < len(steps) else []
        deps = [str(d) for d in raw_deps if str(d).strip()]
        depends_on[step_id] = deps
        for dep in deps:
            if dep not in step_id_set:
                errors.append(f"dag_depends_on_unknown_step:{step_id}->{dep}")
    groups = dag.get("parallel_groups") or []
    for group in groups:
        members = [str(m) for m in group]
        for member in members:
            if member not in step_id_set:
                errors.append(f"dag_parallel_group_unknown_step:{member}")
        for left in members:
            ancestors = _step_ancestors(left, depends_on)
            for right in members:
                if left != right and right in ancestors:
                    errors.append(
                        f"dag_parallel_group_conflicts_with_dependency:{left}<-{right}"
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
    context_pack_hash = str(plan.get("context_pack_hash") or "").strip()
    mapper_pack_hash = str(plan.get("mapper_pack_hash") or "").strip()
    if strict_identity and not context_pack_hash:
        errors.append("context_pack_hash_missing")
    if context_pack_hash and mapper_pack_hash and context_pack_hash != mapper_pack_hash:
        errors.append("context_pack_hash_mismatch")
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
        # Tolerate both object form {"id": ...} and bare-string form so a
        # hand-written contract (or the selftest) never crashes the validator
        # with AttributeError -- it yields a clean schema error instead.
        def _ids(seq):
            out = set()
            for item in seq or []:
                if isinstance(item, Mapping):
                    if item.get("id"):
                        out.add(str(item["id"]))
                elif isinstance(item, str) and item.strip():
                    out.add(item.strip())
            return out

        expected_scenarios = _ids(task.get("scenarios"))
        expected_rules = _ids(task.get("rules"))
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
        for scenario in step.get("steps") or []:
            if not isinstance(scenario, Mapping):
                continue
            scenario_id = str(scenario.get("scenario_id") or scenario.get("id") or "")
            if not scenario_id:
                continue
            scenario_plan = scenario.get("plan") or {}
            no_code_change = bool(scenario_plan.get("no_code_change"))
            if not no_code_change and not scenario_plan.get("read_paths"):
                errors.append(f"task[{index}] scenario_unplanned_read:{scenario_id}")
            if not no_code_change and not scenario_plan.get("change_paths"):
                errors.append(f"task[{index}] scenario_unplanned_change:{scenario_id}")
            if not no_code_change and not scenario_plan.get("test_commands"):
                errors.append(f"task[{index}] scenario_unplanned_test:{scenario_id}")
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
            to_create = {str(item).replace("\\", "/").strip() for item in step.get("to_create") or []}
            if not path.exists() and value not in to_create:
                errors.append(f"task[{index}] target_missing_without_to_create:{value}")

    _validate_dag(plan, steps, errors)

    return {
        "schema": "simplicio.plan-validation/v1",
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "checked_tasks": len(tasks),
    }


__all__ = ["PLAN_SCHEMA", "validate_plan"]
