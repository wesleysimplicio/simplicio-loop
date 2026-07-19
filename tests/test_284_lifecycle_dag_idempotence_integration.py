"""Tests for the four remaining #284 follow-up gaps flagged after PR #373:

1. Wiring ``planning_gate.build_planning_receipt()`` into the REAL ``arm_run()``
   dispatch path (opt-in, so every existing gate test keeps its current
   behavior unchanged) -- ``simplicio_loop/runner.py::_maybe_auto_build_planning_receipt``.
2. Plan v2's own DAG/parallelizable-step schema field --
   ``simplicio_loop/plan_contract.py::_validate_dag``.
3. Crash/retry idempotence across the intake boundary -- a resume after a
   simulated crash must reuse the existing planning receipt (same
   ``plan_revision``) instead of re-planning from scratch, unless drift is
   genuinely detected.
"""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from simplicio_loop import runner as runner_mod
from simplicio_loop.plan_contract import validate_plan
from simplicio_loop.planning_gate import (
    build_planning_receipt,
    evaluate_mutation_authority,
    load_planning_receipt,
    receipt_path,
    replan_on_drift,
)

from tests.test_runner_cli_integration import _arm_deterministic_preflight_fixture

AUTO_FLAG = "SIMPLICIO_LOOP_AUTO_PLANNING_RECEIPT"


# ---------------------------------------------------------------------------
# 1. Real dispatch-path wiring: arm_run() auto-builds the planning receipt
# ---------------------------------------------------------------------------

def test_arm_run_does_not_auto_build_receipt_when_explicitly_disabled(tmp_path, monkeypatch):
    """#284 mandatory-by-default flip: an explicit falsy value is the ONLY way to
    keep the legacy (pre-flip) no-receipt behavior. This is what a caller that
    truly cannot satisfy the gate yet must set (mirrors
    ``SIMPLICIO_REQUIRE_MUTATION_AUTHORITY``'s opt-out polarity, #284/#360)."""
    monkeypatch.setenv(AUTO_FLAG, "0")
    _, _, armed, run_dir = _arm_deterministic_preflight_fixture(monkeypatch, tmp_path)
    assert armed["state"]["phase"] == "awaiting_decision"
    assert not receipt_path(run_dir).exists()


def test_arm_run_auto_builds_valid_receipt_by_default(tmp_path, monkeypatch):
    """Mandatory-by-default (#284 follow-up): with the flag unset, the REAL
    arm_run() dispatch path builds a planning-receipt.json whose mutation
    authority is immediately valid for the current run/attempt/contract/plan
    identity -- no external ``scripts/planning_gate.py build`` call or
    ``stage_valid_planning_receipt()`` test fixture required."""
    monkeypatch.delenv(AUTO_FLAG, raising=False)
    repo, _, armed, run_dir = _arm_deterministic_preflight_fixture(monkeypatch, tmp_path)
    assert armed["state"]["phase"] == "awaiting_decision"

    receipt = load_planning_receipt(run_dir)
    assert receipt is not None
    assert receipt["ready_for_mutation"] is True
    assert receipt["mutation_authority"]

    contract = json.loads((run_dir / "task-contract.json").read_text(encoding="utf-8"))
    plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    verdict = evaluate_mutation_authority(
        run_dir, run_id=armed["manifest"]["run_id"], attempt=1,
        task_contract_hash=contract["collection_hash"],
        plan_hash=receipt["plan_hash"],
    )
    assert verdict["ok"] is True, verdict


def test_arm_run_auto_receipt_lets_execute_operator_run_without_manual_fixture(tmp_path, monkeypatch):
    """End-to-end proof: with the flag on, `execute_operator()` -- which is
    mandatory-by-default gated on a valid `planning-receipt.json` -- succeeds
    using ONLY the receipt `arm_run()` itself produced."""
    monkeypatch.setenv(AUTO_FLAG, "1")
    repo, _, armed, run_dir = _arm_deterministic_preflight_fixture(monkeypatch, tmp_path)
    run_id = armed["manifest"]["run_id"]
    exec_env = {
        "SIMPLICIO_LOOP_FAKE_OPERATOR_EXEC_JSON": json.dumps({
            "returncode": 0, "stdout": {"kind": "operator-applied", "ok": True}, "stderr": "",
            "write_files": {"src/app.py": "def main():\n    return 'updated'\n"},
        }),
    }
    with patch.dict(os.environ, exec_env, clear=False):
        payload = runner_mod.execute_operator(str(repo), run_id)
    assert payload["state"]["phase"] == "validating"
    op_receipt = json.loads((run_dir / "operator-receipt.json").read_text(encoding="utf-8"))
    assert op_receipt["execution_state"] == "applied"


def test_arm_run_auto_receipt_no_github_publish_without_source_issue(tmp_path, monkeypatch):
    """No GitHub `source_issue` on the run state -> no publish attempt, no crash;
    the receipt is still built purely locally."""
    monkeypatch.setenv(AUTO_FLAG, "1")
    monkeypatch.setenv("SIMPLICIO_LOOP_GITHUB_LIFECYCLE_SYNC", "1")
    _, _, armed, run_dir = _arm_deterministic_preflight_fixture(monkeypatch, tmp_path)
    receipt = load_planning_receipt(run_dir)
    assert receipt is not None
    assert "source" not in receipt
    assert not (run_dir / "lifecycle-sync-errors.jsonl").exists() or all(
        "planning_receipt_auto_build" not in line
        for line in (run_dir / "lifecycle-sync-errors.jsonl").read_text(encoding="utf-8").splitlines()
    )


# ---------------------------------------------------------------------------
# 2. Plan v2 DAG / parallelizable-step schema field
# ---------------------------------------------------------------------------

CONTRACT_3TASK = {
    "schema": "simplicio.task-contract-collection/v1",
    "collection_hash": "c-dag",
    "tasks": [
        {"id": "T1", "scenarios": [{"id": "SCN1", "title": "A"}], "rules": []},
        {"id": "T2", "scenarios": [{"id": "SCN2", "title": "B"}], "rules": []},
        {"id": "T3", "scenarios": [{"id": "SCN3", "title": "C"}], "rules": []},
    ],
}


def _plan_with_dag(dag, depends=None):
    depends = depends or {}
    return {
        "schema": "simplicio.plan/v1", "task_contract_hash": "c-dag",
        "mapper_pack_hash": "mp1", "context_pack_hash": "mp1",
        "repo_state": {"head": "h1", "tree_hash": "t1"},
        "freshness": {"verified": True, "current_state": {"head": "h1", "tree_hash": "t1"}},
        "dag": dag,
        "steps": [
            {
                "id": "T1", "candidate_targets": ["a.py"], "to_create": ["a.py"],
                "depends_on": depends.get("T1", []),
                "steps": [{"scenario_id": "SCN1", "plan": {"no_code_change": True, "no_code_change_reason": "n/a"}}],
            },
            {
                "id": "T2", "candidate_targets": ["b.py"], "to_create": ["b.py"],
                "depends_on": depends.get("T2", []),
                "steps": [{"scenario_id": "SCN2", "plan": {"no_code_change": True, "no_code_change_reason": "n/a"}}],
            },
            {
                "id": "T3", "candidate_targets": ["c.py"], "to_create": ["c.py"],
                "depends_on": depends.get("T3", []),
                "steps": [{"scenario_id": "SCN3", "plan": {"no_code_change": True, "no_code_change_reason": "n/a"}}],
            },
        ],
    }


def test_dag_marks_two_independent_steps_parallelizable():
    plan = _plan_with_dag({"parallel_groups": [["T1", "T2"]]})
    verdict = validate_plan(plan, CONTRACT_3TASK["tasks"], ".", contract_hash="c-dag",
                            current_state={"head": "h1", "tree_hash": "t1"})
    assert verdict["valid"], verdict["errors"]
    assert not any(e.startswith("dag_") for e in verdict["errors"])


def test_dag_rejects_dependent_steps_mislabeled_as_parallel():
    # T3 depends on T1 -- grouping them as parallelizable is a real conflict.
    plan = _plan_with_dag({"parallel_groups": [["T1", "T3"]]}, depends={"T3": ["T1"]})
    verdict = validate_plan(plan, CONTRACT_3TASK["tasks"], ".", contract_hash="c-dag",
                            current_state={"head": "h1", "tree_hash": "t1"})
    assert not verdict["valid"]
    assert any("dag_parallel_group_conflicts_with_dependency" in e for e in verdict["errors"])


def test_dag_rejects_transitive_dependency_in_parallel_group():
    # T3 depends on T2 which depends on T1: T1/T3 conflict transitively.
    plan = _plan_with_dag({"parallel_groups": [["T1", "T3"]]},
                          depends={"T2": ["T1"], "T3": ["T2"]})
    verdict = validate_plan(plan, CONTRACT_3TASK["tasks"], ".", contract_hash="c-dag",
                            current_state={"head": "h1", "tree_hash": "t1"})
    assert not verdict["valid"]
    assert any("dag_parallel_group_conflicts_with_dependency" in e for e in verdict["errors"])


def test_dag_absent_is_a_pure_no_op():
    plan = _plan_with_dag({})
    del plan["dag"]
    verdict = validate_plan(plan, CONTRACT_3TASK["tasks"], ".", contract_hash="c-dag",
                            current_state={"head": "h1", "tree_hash": "t1"})
    assert verdict["valid"], verdict["errors"]


def test_dag_flags_unknown_step_in_parallel_group():
    plan = _plan_with_dag({"parallel_groups": [["T1", "T-GHOST"]]})
    verdict = validate_plan(plan, CONTRACT_3TASK["tasks"], ".", contract_hash="c-dag",
                            current_state={"head": "h1", "tree_hash": "t1"})
    assert not verdict["valid"]
    assert any("dag_parallel_group_unknown_step" in e for e in verdict["errors"])


def test_dag_flags_unknown_depends_on_target():
    plan = _plan_with_dag({"parallel_groups": []}, depends={"T2": ["T-GHOST"]})
    verdict = validate_plan(plan, CONTRACT_3TASK["tasks"], ".", contract_hash="c-dag",
                            current_state={"head": "h1", "tree_hash": "t1"})
    assert not verdict["valid"]
    assert any("dag_depends_on_unknown_step" in e for e in verdict["errors"])


# ---------------------------------------------------------------------------
# 3. Crash/retry idempotence across the intake boundary
# ---------------------------------------------------------------------------

CONTRACT = {
    "schema": "simplicio.task-contract-collection/v1",
    "collection_hash": "c1",
    "tasks": [{
        "id": "T1",
        "scenarios": [{"id": "SCN1", "title": "Faz X", "given": ["algo"], "when": ["evento"],
                       "then": ["resultado"], "rule_refs": ["RN1"]}],
        "rules": [{"id": "RN1", "text": "regra", "scenario_refs": ["SCN1"]}],
    }],
}
PLAN = {
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


def _validation():
    return validate_plan(PLAN, CONTRACT["tasks"], ".", contract_hash=CONTRACT["collection_hash"],
                         current_state={"head": "h1", "tree_hash": "t1"})


def test_crash_after_receipt_write_resumes_by_reusing_same_receipt(tmp_path):
    """Simulate: intake receipt is written to disk (planning finished), then the
    process crashes BEFORE execution starts. On resume, the caller must
    re-verify the SAME on-disk receipt (same plan_revision, same
    mutation_authority) instead of unconditionally rebuilding/re-planning --
    rebuilding on every resume would mint a fresh authority even when nothing
    changed, defeating the point of a durable receipt."""
    validation = _validation()
    receipt = build_planning_receipt(
        run_id="run-crash", attempt=1, contract=CONTRACT, plan=PLAN,
        plan_validation=validation, source_snapshot=SOURCE_A,
    )
    receipt_path(tmp_path).write_text(json.dumps(receipt), encoding="utf-8")
    assert receipt["plan_revision"] == 0

    # -- simulated crash: process exits here, nothing else persisted --

    # -- resume: reload from disk, do NOT rebuild/replan --
    resumed = load_planning_receipt(tmp_path)
    assert resumed["plan_revision"] == receipt["plan_revision"]
    assert resumed["mutation_authority"] == receipt["mutation_authority"]
    assert resumed["task_contract_hash"] == receipt["task_contract_hash"]

    verdict = evaluate_mutation_authority(
        tmp_path, run_id="run-crash", attempt=1,
        task_contract_hash=receipt["task_contract_hash"], plan_hash=receipt["plan_hash"],
        source_snapshot_hash="hash-a",
    )
    assert verdict["ok"] is True, verdict


def test_resume_after_crash_with_genuine_drift_does_not_silently_reuse_stale_receipt(tmp_path):
    """The mirror case: if the source genuinely changed while the process was
    down, blind reuse of the crashed receipt must NOT be treated as valid --
    only an explicit `replan_on_drift()` call may mint a new one."""
    validation = _validation()
    receipt = build_planning_receipt(
        run_id="run-crash2", attempt=1, contract=CONTRACT, plan=PLAN,
        plan_validation=validation, source_snapshot=SOURCE_A,
    )
    receipt_path(tmp_path).write_text(json.dumps(receipt), encoding="utf-8")

    # -- simulated crash, then resume with a freshly re-queried source that has drifted --
    verdict = evaluate_mutation_authority(
        tmp_path, run_id="run-crash2", attempt=1,
        task_contract_hash=receipt["task_contract_hash"], plan_hash=receipt["plan_hash"],
        source_snapshot_hash="hash-DRIFTED",
    )
    assert verdict["ok"] is False
    assert verdict["reason_code"] == "source_drift"

    # the ONLY sanctioned recovery is an explicit replan, which bumps the revision
    # and preserves the crash-time receipt's history in the diff.
    source_b = {"schema": "simplicio.source-snapshot/v1",
                "source": {"provider": "github", "repo": "acme/repo", "item_id": "284",
                           "revision": "r2", "snapshot_hash": "hash-DRIFTED",
                           "observed_at": "2026-01-02T00:00:00Z"}}
    replanned = replan_on_drift(
        tmp_path, run_id="run-crash2", attempt=2, contract=CONTRACT, plan=PLAN,
        plan_validation=validation, baseline_source_snapshot=SOURCE_A, current_source_snapshot=source_b,
    )
    assert replanned["plan_revision"] == 1
    assert replanned["replan"]["drift_detected"] is True
    assert replanned["replan"]["diff"]["previous_plan_hash"] == receipt["plan_hash"]
    on_disk = load_planning_receipt(tmp_path)
    assert on_disk["plan_revision"] == 1


def test_resume_twice_without_drift_is_idempotent_and_never_bumps_revision(tmp_path):
    """Resuming the same crashed run repeatedly (e.g. a flaky scheduler retrying
    the resume step) must not itself bump plan_revision -- only a genuine
    `replan_on_drift()` call may do that."""
    validation = _validation()
    receipt = build_planning_receipt(
        run_id="run-crash3", attempt=1, contract=CONTRACT, plan=PLAN,
        plan_validation=validation, source_snapshot=SOURCE_A,
    )
    receipt_path(tmp_path).write_text(json.dumps(receipt), encoding="utf-8")

    for _ in range(3):
        resumed = load_planning_receipt(tmp_path)
        assert resumed["plan_revision"] == 0
        verdict = evaluate_mutation_authority(
            tmp_path, run_id="run-crash3", attempt=1,
            task_contract_hash=receipt["task_contract_hash"], plan_hash=receipt["plan_hash"],
            source_snapshot_hash="hash-a",
        )
        assert verdict["ok"] is True
    assert load_planning_receipt(tmp_path)["plan_revision"] == 0
