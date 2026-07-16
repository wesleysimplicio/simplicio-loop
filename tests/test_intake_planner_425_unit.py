"""Unit tests for the #425 `intake_planner` concrete stage-agent role.

Builds on the #284 fixtures (task contract / plan / intake / matrix) already
used by `tests/test_intake_impact_matrix_replan_284_unit.py`, and covers the
#425-specific checklist: the typed `intake-planner-receipt`, boundary
enforcement (no product-code mutation / no commit-PR-merge), the risk
register gate, the dependency DAG explicitness, the impact-gap threshold, and
the single-clarifying-question path.
"""
from __future__ import annotations

import json

import pytest

from simplicio_loop.intake_contract import build_task_intake
from simplicio_loop.traceability_matrix import build_matrix
from simplicio_loop.intake_planner import (
    INTAKE_PLANNER_RECEIPT_SCHEMA,
    INTAKE_PLANNER_ROLE_ID,
    VERDICT_BLOCKED,
    VERDICT_PASSED,
    IntakePlannerBoundaryError,
    assert_boundary_ok,
    build_dependency_dag,
    build_intake_planner_receipt,
    build_risk_register,
    impact_gap_ok,
    is_path_in_boundary,
    receipt_is_passed,
    to_stage_receipt,
)

CONTRACT = {
    "schema": "simplicio.task-contract-collection/v1",
    "collection_hash": "c1",
    "tasks": [{
        "id": "T1",
        "scenarios": [
            {"id": "SCN1", "title": "Does X", "given": ["something"], "when": ["event"],
             "then": ["result"], "rule_refs": ["RN1"]},
        ],
        "rules": [{"id": "RN1", "text": "rule", "scenario_refs": ["SCN1"]}],
    }],
}

PLAN_COVERED = {
    "schema": "simplicio.plan/v1", "task_contract_hash": "c1",
    "mapper_pack_hash": "mp1", "context_pack_hash": "mp1",
    "repo_state": {"head": "h1", "tree_hash": "t1"},
    "freshness": {"verified": True, "current_state": {"head": "h1", "tree_hash": "t1"}},
    "steps": [{
        "id": "T1", "candidate_targets": ["a.py"], "to_create": ["a.py"], "rule_ids": ["RN1"],
        "steps": [{
            "scenario_id": "SCN1", "rule_ids": ["RN1"],
            "plan": {"read_paths": ["a.py"], "change_paths": ["a.py"],
                     "test_commands": ["pytest tests/test_a.py"]},
        }],
    }],
}

SOURCE = {
    "schema": "simplicio.source-snapshot/v1",
    "source": {"provider": "github", "repo": "acme/repo", "item_id": "425",
               "revision": "r1", "snapshot_hash": "hash-a", "observed_at": "2026-01-01T00:00:00Z"},
}

PLAN_VALIDATION_OK = {"valid": True, "errors": [], "warnings": [], "checked_tasks": 1}

RISK_OK = [{"id": "R1", "text": "risk", "mitigation": "mitigate it"}]
DEPS_OK = [{"id": "D1", "depends_on": [], "state": "resolved"}]


def _intake(delivery_target="implemented"):
    return build_task_intake(run_id="r1", attempt=1, contract=CONTRACT, plan_hash="p1",
                              source_snapshot=SOURCE, delivery_target=delivery_target)


def _matrix():
    return build_matrix(CONTRACT, PLAN_COVERED)


def _base_kwargs(**overrides):
    kwargs = dict(
        run_id="r1", attempt=1, contract=CONTRACT, plan=PLAN_COVERED,
        plan_validation=PLAN_VALIDATION_OK, intake=_intake(), traceability_matrix=_matrix(),
        source_snapshot=SOURCE, risks=RISK_OK, dependencies=DEPS_OK,
        conventions_consulted=True, precedents_consulted=True,
    )
    kwargs.update(overrides)
    return kwargs


# --------------------------------------------------------------------------- #
# Happy path: PASSED only when every gate condition holds
# --------------------------------------------------------------------------- #
def test_receipt_passed_when_every_gate_condition_holds():
    receipt = build_intake_planner_receipt(**_base_kwargs())
    assert receipt["schema"] == INTAKE_PLANNER_RECEIPT_SCHEMA
    assert receipt["role_id"] == INTAKE_PLANNER_ROLE_ID
    assert receipt["verdict"] == VERDICT_PASSED
    assert receipt["failing_checks"] == []
    assert receipt_is_passed(receipt)
    assert all(receipt["checklist"].values())
    assert receipt["receipt_hash"]


def test_receipt_blocked_when_source_snapshot_missing():
    receipt = build_intake_planner_receipt(**_base_kwargs(source_snapshot=None))
    assert receipt["verdict"] == VERDICT_BLOCKED
    assert "source_read_and_revision_frozen" in receipt["failing_checks"]


def test_receipt_blocked_when_ac_step_evidence_gap():
    gap_plan = json.loads(json.dumps(PLAN_COVERED))
    gap_plan["steps"][0]["steps"][0]["plan"]["test_commands"] = []
    matrix = build_matrix(CONTRACT, gap_plan)
    receipt = build_intake_planner_receipt(**_base_kwargs(plan=gap_plan, traceability_matrix=matrix))
    assert receipt["verdict"] == VERDICT_BLOCKED
    assert "every_ac_has_step_and_proof" in receipt["failing_checks"]
    assert "every_step_maps_to_ac" in receipt["failing_checks"]


def test_receipt_blocked_when_delivery_target_missing():
    intake = _intake()
    intake["understanding"]["delivery_target"] = ""
    receipt = build_intake_planner_receipt(**_base_kwargs(intake=intake))
    assert receipt["verdict"] == VERDICT_BLOCKED
    assert "delivery_target_defined" in receipt["failing_checks"]


def test_receipt_blocked_when_risk_has_no_mitigation_or_blocker():
    risks = [{"id": "R1", "text": "unmitigated risk"}]
    receipt = build_intake_planner_receipt(**_base_kwargs(risks=risks))
    assert receipt["verdict"] == VERDICT_BLOCKED
    assert "risks_mitigated_or_blocked" in receipt["failing_checks"]
    assert "risk_missing_mitigation_or_blocker:R1" in receipt["risk_register"]["errors"]


def test_risk_with_is_blocker_true_and_no_mitigation_still_passes_gate():
    result = build_risk_register([{"id": "R1", "text": "known blocker", "is_blocker": True}])
    assert result["ok"]
    assert result["errors"] == []


def test_receipt_blocked_when_conventions_not_consulted():
    receipt = build_intake_planner_receipt(**_base_kwargs(conventions_consulted=False))
    assert receipt["verdict"] == VERDICT_BLOCKED
    assert "architecture_conventions_consulted" in receipt["failing_checks"]


def test_receipt_blocked_when_impact_gap_above_threshold():
    impact_map = {"issues": [{"severity": "high", "text": "cross-module blast radius"}]}
    receipt = build_intake_planner_receipt(**_base_kwargs(impact_map=impact_map))
    assert receipt["verdict"] == VERDICT_BLOCKED
    assert "impact_audit_below_threshold" in receipt["failing_checks"]


def test_impact_gap_ok_ignores_low_and_medium_severities_by_default():
    impact_map = {"issues": [{"severity": "medium"}, {"severity": "low"}]}
    assert impact_gap_ok(impact_map) is True


def test_impact_gap_ok_true_when_no_impact_map_supplied():
    assert impact_gap_ok(None) is True


# --------------------------------------------------------------------------- #
# Single clarifying question -- BLOCKED(needs_clarification), not a silent guess
# --------------------------------------------------------------------------- #
def test_needs_clarification_blocks_even_an_otherwise_perfect_plan():
    receipt = build_intake_planner_receipt(
        **_base_kwargs(needs_clarification=True, clarification_question="Which delivery target applies?")
    )
    assert receipt["verdict"] == VERDICT_BLOCKED
    assert receipt["needs_clarification"] is True
    assert receipt["clarification_question"] == "Which delivery target applies?"
    assert "no_clarification_pending" in receipt["failing_checks"]
    # the underlying planning_gate receipt is also AWAITING_DECISION, never COMPLETE
    assert receipt["planning_receipt"]["verdict"] == "AWAITING_DECISION"
    assert receipt["planning_receipt"]["ready_for_mutation"] is False


# --------------------------------------------------------------------------- #
# Dependency DAG explicitness
# --------------------------------------------------------------------------- #
def test_dependency_dag_marks_unresolved_dependency_as_blocked_explicitly():
    deps = [
        {"id": "D1", "depends_on": [], "state": "open"},
        {"id": "D2", "depends_on": ["D1"], "state": "open"},
    ]
    dag = build_dependency_dag(deps)
    assert "D2" in dag["blocked_ids"]
    assert dag["has_blocked"] is True
    assert dag["dag_hash"]


def test_dependency_dag_all_resolved_has_no_blocked_ids():
    deps = [{"id": "D1", "depends_on": [], "state": "resolved"}]
    dag = build_dependency_dag(deps)
    assert dag["blocked_ids"] == []
    assert dag["has_blocked"] is False


# --------------------------------------------------------------------------- #
# Boundary enforcement -- "Não pode alterar código do produto / commit/PR/merge"
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", [
    ".orchestrator/loop/PROGRESS.md",
    ".simplicio/context.json",
    "task-intake.json",
    "planning-receipt.json",
    "ac-matrix.json",
    "impact-map.json",
])
def test_allowlisted_artifact_paths_are_in_boundary(path):
    assert is_path_in_boundary(path) is True


@pytest.mark.parametrize("path", [
    "simplicio_loop/runner.py",
    "src/app/main.py",
    "README.md",
    ".git/refs/heads/main",
])
def test_product_code_paths_are_out_of_boundary(path):
    assert is_path_in_boundary(path) is False


def test_assert_boundary_ok_raises_on_product_code_path():
    with pytest.raises(IntakePlannerBoundaryError):
        assert_boundary_ok(["simplicio_loop/runner.py"])


@pytest.mark.parametrize("verb", ["commit", "pr", "push", "merge"])
def test_assert_boundary_ok_rejects_commit_pr_merge_verbs(verb):
    with pytest.raises(IntakePlannerBoundaryError):
        assert_boundary_ok([verb])


def test_assert_boundary_ok_allows_only_allowlisted_paths():
    assert_boundary_ok(["task-intake.json", "planning-receipt.json"])  # no raise


def test_build_intake_planner_receipt_raises_when_touched_paths_out_of_boundary():
    with pytest.raises(IntakePlannerBoundaryError):
        build_intake_planner_receipt(**_base_kwargs(touched_paths=["simplicio_loop/runner.py"]))


def test_build_intake_planner_receipt_ok_when_touched_paths_in_boundary():
    receipt = build_intake_planner_receipt(**_base_kwargs(touched_paths=["task-intake.json"]))
    assert receipt["verdict"] == VERDICT_PASSED


# --------------------------------------------------------------------------- #
# Stage-agent binding: projects into the portable StageReceipt shape
# --------------------------------------------------------------------------- #
def test_to_stage_receipt_projects_passed_verdict_as_pass():
    receipt = build_intake_planner_receipt(**_base_kwargs())
    stage_receipt = to_stage_receipt(
        receipt, receipt_id="rec-1", agent_instance_id="inst-1",
        task_id="task-1", attempt_id="att-1", fence="fence-1",
    )
    assert stage_receipt["schema"] == "simplicio.stage-receipt/v1"
    assert stage_receipt["role_id"] == INTAKE_PLANNER_ROLE_ID
    assert stage_receipt["stage_id"] == "intake"
    assert stage_receipt["verdict"] == "pass"
    assert stage_receipt["artifact_hash"] == receipt["receipt_hash"]


def test_to_stage_receipt_projects_blocked_verdict():
    receipt = build_intake_planner_receipt(**_base_kwargs(source_snapshot=None))
    stage_receipt = to_stage_receipt(
        receipt, receipt_id="rec-2", agent_instance_id="inst-2",
        task_id="task-1", attempt_id="att-1", fence="fence-1",
    )
    assert stage_receipt["verdict"] == "blocked"


# --------------------------------------------------------------------------- #
# The intake_planner role is registered in the manifesto with a boundary
# (regression guard against the manifesto drifting away from this module).
# --------------------------------------------------------------------------- #
def test_manifesto_registers_intake_planner_role_and_intake_stage():
    from simplicio_loop import stage_agents as sa
    graph = sa.load_graph()
    roles = {r["role_id"]: r for r in graph["roles"]}
    stages = {s["stage_id"]: s for s in graph["stages"]}
    assert INTAKE_PLANNER_ROLE_ID in roles
    assert stages["intake"]["role_id"] == INTAKE_PLANNER_ROLE_ID
