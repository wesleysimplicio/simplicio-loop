"""Unit tests for the #284 follow-up gaps: the `task-intake/v1` envelope,
the AC<->step<->test<->evidence matrix, the impact-map wiring, and the
genuine replan-on-drift path.

These land on top of the already-merged planning-receipt/mutation-authority
gate (`simplicio_loop/planning_gate.py`, #329/#360) and stay strictly
additive: every existing planning-gate fixture/test keeps passing unchanged
(`build_planning_receipt()`'s three new params all default to `None`/`0`).
"""
import json

from simplicio_loop.intake_contract import (
    INTAKE_SCHEMA,
    build_task_intake,
    lint_task_intake,
)
from simplicio_loop.plan_contract import validate_plan
from simplicio_loop.planning_gate import (
    build_planning_receipt,
    content_hash,
    load_planning_receipt,
    receipt_path,
    replan_on_drift,
)
from simplicio_loop.traceability_matrix import MATRIX_SCHEMA, build_matrix

CONTRACT = {
    "schema": "simplicio.task-contract-collection/v1",
    "collection_hash": "c1",
    "tasks": [{
        "id": "T1",
        "scenarios": [
            {"id": "SCN1", "title": "Faz X", "given": ["algo"], "when": ["evento"],
             "then": ["resultado"], "rule_refs": ["RN1"]},
        ],
        "rules": [{"id": "RN1", "text": "regra", "scenario_refs": ["SCN1"]}],
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

SOURCE_A = {"schema": "simplicio.source-snapshot/v1",
            "source": {"provider": "github", "repo": "acme/repo", "item_id": "284",
                       "revision": "r1", "snapshot_hash": "hash-a", "observed_at": "2026-01-01T00:00:00Z"}}
SOURCE_B = {"schema": "simplicio.source-snapshot/v1",
            "source": {"provider": "github", "repo": "acme/repo", "item_id": "284",
                       "revision": "r2", "snapshot_hash": "hash-b", "observed_at": "2026-01-02T00:00:00Z"}}


def _gap_plan():
    plan = json.loads(json.dumps(PLAN_COVERED))
    plan["steps"][0]["steps"][0]["plan"]["test_commands"] = []
    return plan


# -- task-intake/v1 envelope -------------------------------------------------

def test_build_task_intake_projects_every_source_scenario_as_an_ac():
    intake = build_task_intake(run_id="r1", attempt=1, contract=CONTRACT, plan_hash="p1",
                               delivery_target="verified")
    assert intake["schema"] == INTAKE_SCHEMA
    assert [ac["id"] for ac in intake["acceptance_criteria"]] == ["SCN1"]
    ac = intake["acceptance_criteria"][0]
    assert ac["origin"] == "source"
    assert ac["state"] == "pending"
    assert ac["text"] == "Faz X"
    assert intake["understanding"]["delivery_target"] == "verified"
    assert intake["hashes"]["task_contract_hash"] == "c1"


def test_build_task_intake_hash_is_deterministic():
    a = build_task_intake(run_id="r1", attempt=1, contract=CONTRACT, plan_hash="p1")
    b = build_task_intake(run_id="r1", attempt=1, contract=CONTRACT, plan_hash="p1")
    assert a["intake_hash"] == b["intake_hash"]
    c = build_task_intake(run_id="r1", attempt=2, contract=CONTRACT, plan_hash="p1")
    assert c["intake_hash"] != a["intake_hash"]


def test_lint_task_intake_fails_closed_without_acceptance_criteria():
    intake = build_task_intake(run_id="r1", attempt=1, contract=CONTRACT, plan_hash="p1")
    intake_no_ac = dict(intake, acceptance_criteria=[])
    verdict = lint_task_intake(intake_no_ac)
    assert verdict["valid"] is False
    assert "no_acceptance_criteria" in verdict["errors"]


def test_lint_task_intake_flags_vague_or_originless_ac():
    intake = build_task_intake(run_id="r1", attempt=1, contract=CONTRACT, plan_hash="p1")
    vague = dict(intake, acceptance_criteria=[{"id": "AC-X", "text": "", "origin": "derived"}])
    verdict = lint_task_intake(vague)
    assert verdict["valid"] is False
    assert any("missing_text" in e for e in verdict["errors"])

    originless = dict(intake, acceptance_criteria=[{"id": "AC-X", "text": "faz algo", "origin": "guessed"}])
    verdict2 = lint_task_intake(originless)
    assert verdict2["valid"] is False
    assert any("missing_origin" in e for e in verdict2["errors"])


def test_build_task_intake_normalizes_unknown_delivery_target_to_implemented():
    intake = build_task_intake(run_id="r1", attempt=1, contract=CONTRACT, plan_hash="p1",
                               delivery_target="not-a-real-target")
    assert intake["understanding"]["delivery_target"] == "implemented"


def test_lint_task_intake_rejects_invalid_delivery_target():
    intake = build_task_intake(run_id="r1", attempt=1, contract=CONTRACT, plan_hash="p1")
    tampered = dict(intake, understanding=dict(intake["understanding"], delivery_target="not-a-real-target"))
    verdict = lint_task_intake(tampered)
    assert verdict["valid"] is False
    assert "delivery_target_invalid" in verdict["errors"]


# -- AC <-> step <-> test <-> evidence matrix --------------------------------

def test_build_matrix_covered_plan_has_no_gaps():
    matrix = build_matrix(CONTRACT, PLAN_COVERED)
    assert matrix["schema"] == MATRIX_SCHEMA
    assert matrix["coverage_ok"] is True
    assert matrix["gaps"] == []
    row = matrix["rows"][0]
    assert row["ac_id"] == "SCN1"
    assert row["test_commands"] == ["pytest tests/test_a.py"]
    assert row["covered"] is True


def test_build_matrix_flags_gap_when_ac_has_no_test_and_no_evidence():
    matrix = build_matrix(CONTRACT, _gap_plan())
    assert matrix["coverage_ok"] is False
    assert matrix["gaps"] == ["SCN1"]
    assert matrix["rows"][0]["covered"] is False
    assert matrix["rows"][0]["evidence_status"] == "missing"


def test_build_matrix_no_code_change_is_not_applicable_with_justification():
    plan = json.loads(json.dumps(PLAN_COVERED))
    plan["steps"][0]["steps"][0]["plan"] = {"no_code_change": True, "no_code_change_reason": "docs only"}
    matrix = build_matrix(CONTRACT, plan)
    assert matrix["coverage_ok"] is True
    row = matrix["rows"][0]
    assert row["evidence_status"] == "not_applicable"
    assert row["evidence_justification"] == "docs only"


def test_matrix_hash_is_deterministic_and_content_bound():
    m1 = build_matrix(CONTRACT, PLAN_COVERED)
    m2 = build_matrix(CONTRACT, PLAN_COVERED)
    assert m1["matrix_hash"] == m2["matrix_hash"]
    m3 = build_matrix(CONTRACT, _gap_plan())
    assert m3["matrix_hash"] != m1["matrix_hash"]


# -- planning-receipt wiring: a gapped matrix blocks mutation ----------------

def _validation_for(plan):
    return validate_plan(plan, CONTRACT["tasks"], ".", contract_hash=CONTRACT["collection_hash"],
                         current_state={"head": "h1", "tree_hash": "t1"})


def test_receipt_with_gapped_matrix_is_not_ready_for_mutation():
    validation = _validation_for(PLAN_COVERED)
    assert validation["valid"], validation["errors"]
    gapped_matrix = build_matrix(CONTRACT, _gap_plan())
    receipt = build_planning_receipt(run_id="r1", attempt=1, contract=CONTRACT, plan=PLAN_COVERED,
                                     plan_validation=validation, traceability_matrix=gapped_matrix)
    assert receipt["ready_for_mutation"] is False
    assert receipt["mutation_authority"] == ""
    assert receipt["traceability_summary"]["gaps"] == ["SCN1"]


def test_receipt_with_covered_matrix_stays_ready():
    validation = _validation_for(PLAN_COVERED)
    covered_matrix = build_matrix(CONTRACT, PLAN_COVERED)
    receipt = build_planning_receipt(run_id="r1", attempt=1, contract=CONTRACT, plan=PLAN_COVERED,
                                     plan_validation=validation, traceability_matrix=covered_matrix)
    assert receipt["ready_for_mutation"] is True
    assert receipt["mutation_authority"]
    assert receipt["traceability_summary"]["coverage_ok"] is True


def test_receipt_folds_in_intake_and_impact_map_hashes():
    validation = _validation_for(PLAN_COVERED)
    intake = build_task_intake(run_id="r1", attempt=1, contract=CONTRACT, plan_hash=content_hash(PLAN_COVERED))
    impact_map = {"schema": "simplicio.impact-audit/v1", "counts": {"seed_files": 1, "issues": 0}}
    receipt = build_planning_receipt(run_id="r1", attempt=1, contract=CONTRACT, plan=PLAN_COVERED,
                                     plan_validation=validation, intake=intake, impact_map=impact_map)
    assert receipt["intake_hash"] == intake["intake_hash"]
    assert receipt["intake_summary"]["acceptance_criteria"] == 1
    assert receipt["impact_map_summary"] == {"seed_files": 1, "issues": 0}


def test_receipt_backward_compatible_without_new_optional_artifacts():
    """No intake/impact_map/traceability_matrix supplied -> identical shape to
    the pre-#284-follow-up receipt (every existing fixture/test relies on this)."""
    validation = _validation_for(PLAN_COVERED)
    receipt = build_planning_receipt(run_id="r1", attempt=1, contract=CONTRACT, plan=PLAN_COVERED,
                                     plan_validation=validation)
    assert receipt["ready_for_mutation"] is True
    assert "intake_hash" not in receipt
    assert "impact_map_hash" not in receipt
    assert "traceability_matrix_hash" not in receipt
    assert receipt["plan_revision"] == 0


# -- genuine replan-on-drift --------------------------------------------------

def test_replan_on_drift_bumps_revision_and_records_diff(tmp_path):
    validation = _validation_for(PLAN_COVERED)
    first = build_planning_receipt(run_id="r-replan", attempt=1, contract=CONTRACT, plan=PLAN_COVERED,
                                   plan_validation=validation, source_snapshot=SOURCE_A)
    receipt_path(tmp_path).write_text(json.dumps(first), encoding="utf-8")
    assert first["plan_revision"] == 0

    replanned = replan_on_drift(
        tmp_path, run_id="r-replan", attempt=2, contract=CONTRACT, plan=PLAN_COVERED,
        plan_validation=validation, baseline_source_snapshot=SOURCE_A, current_source_snapshot=SOURCE_B,
    )
    assert replanned["plan_revision"] == 1
    assert replanned["replan"]["replanned"] is True
    assert replanned["replan"]["drift_detected"] is True
    assert replanned["replan"]["diff"]["source_snapshot_before"] == "hash-a"
    assert replanned["replan"]["diff"]["source_snapshot_after"] == "hash-b"
    assert replanned["ready_for_mutation"] is True
    assert replanned["mutation_authority"]

    on_disk = load_planning_receipt(tmp_path)
    assert on_disk["plan_revision"] == 1


def test_replan_on_drift_without_a_previous_receipt_starts_at_revision_zero(tmp_path):
    validation = _validation_for(PLAN_COVERED)
    replanned = replan_on_drift(
        tmp_path, run_id="r-first", attempt=1, contract=CONTRACT, plan=PLAN_COVERED,
        plan_validation=validation, current_source_snapshot=SOURCE_A,
    )
    assert replanned["plan_revision"] == 0
    assert replanned["replan"]["replanned"] is False


def test_replan_on_drift_never_removes_an_explicit_ac():
    """The replan wrapper packages the SAME contract the caller supplies -- it
    must never drop a `origin=source` scenario/AC on its own."""
    intake_before = build_task_intake(run_id="r1", attempt=1, contract=CONTRACT, plan_hash="p1")
    ac_ids_before = {ac["id"] for ac in intake_before["acceptance_criteria"]}
    validation = _validation_for(PLAN_COVERED)
    receipt = build_planning_receipt(run_id="r1", attempt=1, contract=CONTRACT, plan=PLAN_COVERED,
                                     plan_validation=validation, intake=intake_before)
    intake_after = build_task_intake(run_id="r1", attempt=2, contract=CONTRACT, plan_hash="p1")
    ac_ids_after = {ac["id"] for ac in intake_after["acceptance_criteria"]}
    assert ac_ids_before == ac_ids_after
    assert receipt["intake_summary"]["acceptance_criteria"] == len(ac_ids_before)
