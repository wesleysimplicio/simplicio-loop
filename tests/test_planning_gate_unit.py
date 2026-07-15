"""Unit tests for the #284 planning-receipt / mutation-authority gate.

`simplicio_loop/planning_gate.py` packages an already-computed `validate_plan()`
verdict into one hash-bound `simplicio.planning-receipt/v1` receipt and derives a
mutation-authority token from the run/attempt/contract/plan/lease/fence identity
tuple -- the token that `execute_operator()` can (opt-in, via
SIMPLICIO_REQUIRE_MUTATION_AUTHORITY) require before mutating the repository.
"""
import json

from simplicio_loop.plan_contract import PLAN_SCHEMA, validate_plan
from simplicio_loop.planning_gate import (
    PLANNING_RECEIPT_SCHEMA,
    build_planning_receipt,
    content_hash,
    evaluate_mutation_authority,
    load_planning_receipt,
    mutation_authority_token,
    receipt_path,
    verify_mutation_authority,
)


def _contract():
    return {"schema": "simplicio.task-contract-collection/v1", "collection_hash": "contract-1",
            "tasks": [{"id": "T1", "scenarios": [{"id": "S1"}], "rules": []}]}


def _plan(tmp_path):
    return {
        "schema": PLAN_SCHEMA,
        "task_contract_hash": "contract-1",
        "mapper_pack_hash": "pack-1",
        "context_pack_hash": "pack-1",
        "repo_state": {"head": "head-1", "tree_hash": "tree-1"},
        "freshness": {"verified": True, "current_state": {"head": "head-1", "tree_hash": "tree-1"}},
        "steps": [{
            "candidate_targets": ["src/app.py"], "to_create": ["src/app.py"], "rule_ids": [],
            "steps": [{"scenario_id": "S1", "plan": {
                "read_paths": ["src/app.py"], "change_paths": ["src/app.py"], "test_commands": ["pytest"],
            }}],
        }],
    }


def _valid_plan_validation(tmp_path):
    return validate_plan(_plan(tmp_path), _contract()["tasks"], tmp_path,
                         contract_hash="contract-1",
                         current_state={"head": "head-1", "tree_hash": "tree-1"})


# --- content_hash / token determinism --------------------------------------------


def test_content_hash_is_stable_across_key_order():
    a = content_hash({"x": 1, "y": 2})
    b = content_hash({"y": 2, "x": 1})
    assert a == b


def test_content_hash_differs_on_content_change():
    assert content_hash({"x": 1}) != content_hash({"x": 2})


def test_mutation_authority_token_deterministic():
    t1 = mutation_authority_token(run_id="r1", attempt=1, task_contract_hash="c1", plan_hash="p1")
    t2 = mutation_authority_token(run_id="r1", attempt=1, task_contract_hash="c1", plan_hash="p1")
    assert t1 == t2


def test_mutation_authority_token_changes_with_any_identity_field():
    base = dict(run_id="r1", attempt=1, task_contract_hash="c1", plan_hash="p1",
                lease_id="l1", fencing_token="1")
    base_token = mutation_authority_token(**base)
    for field, new_value in (("run_id", "r2"), ("attempt", 2), ("task_contract_hash", "c2"),
                              ("plan_hash", "p2"), ("lease_id", "l2"), ("fencing_token", "2")):
        variant = dict(base, **{field: new_value})
        assert mutation_authority_token(**variant) != base_token, field


def test_verify_mutation_authority_accepts_matching_and_rejects_mismatched():
    token = mutation_authority_token(run_id="r1", attempt=1, task_contract_hash="c1", plan_hash="p1")
    assert verify_mutation_authority(token, run_id="r1", attempt=1, task_contract_hash="c1", plan_hash="p1")
    assert not verify_mutation_authority(token, run_id="r1", attempt=2, task_contract_hash="c1", plan_hash="p1")
    assert not verify_mutation_authority("", run_id="r1", attempt=1, task_contract_hash="c1", plan_hash="p1")
    assert not verify_mutation_authority(None, run_id="r1", attempt=1, task_contract_hash="c1", plan_hash="p1")


# --- build_planning_receipt --------------------------------------------------------


def test_build_planning_receipt_ready_when_plan_valid(tmp_path):
    plan_validation = _valid_plan_validation(tmp_path)
    assert plan_validation["valid"] is True
    receipt = build_planning_receipt(run_id="run-1", attempt=1, contract=_contract(),
                                     plan=_plan(tmp_path), plan_validation=plan_validation,
                                     lease_id="lease-1", fencing_token="7")
    assert receipt["schema"] == PLANNING_RECEIPT_SCHEMA
    assert receipt["ready_for_mutation"] is True
    assert receipt["mutation_authority"] != ""
    assert receipt["task_contract_hash"] == "contract-1"
    assert receipt["plan_validation"]["valid"] is True


def test_build_planning_receipt_never_mints_authority_when_plan_invalid(tmp_path):
    bad_validation = {"valid": False, "errors": ["task_step_count_mismatch"], "warnings": [], "checked_tasks": 1}
    receipt = build_planning_receipt(run_id="run-1", attempt=1, contract=_contract(),
                                     plan=_plan(tmp_path), plan_validation=bad_validation)
    assert receipt["ready_for_mutation"] is False
    assert receipt["mutation_authority"] == ""


def test_build_planning_receipt_uses_content_hash_when_contract_has_no_collection_hash(tmp_path):
    contract = {"schema": "simplicio.task-contract-collection/v1", "tasks": []}
    receipt = build_planning_receipt(run_id="run-1", attempt=1, contract=contract, plan={},
                                     plan_validation={"valid": True})
    assert receipt["task_contract_hash"] == content_hash(contract)


# --- evaluate_mutation_authority (the fail-closed re-check) ------------------------


def test_evaluate_mutation_authority_missing_receipt_blocks(tmp_path):
    verdict = evaluate_mutation_authority(tmp_path, run_id="run-1", attempt=1,
                                          task_contract_hash="c1", plan_hash="p1")
    assert verdict["ok"] is False
    assert verdict["reason_code"] == "planning_receipt_missing"


def test_evaluate_mutation_authority_round_trips_through_disk(tmp_path):
    plan_validation = _valid_plan_validation(tmp_path)
    receipt = build_planning_receipt(run_id="run-1", attempt=1, contract=_contract(),
                                     plan=_plan(tmp_path), plan_validation=plan_validation,
                                     lease_id="lease-1", fencing_token="7")
    receipt_path(tmp_path).write_text(json.dumps(receipt), encoding="utf-8")
    loaded = load_planning_receipt(tmp_path)
    assert loaded == receipt

    verdict = evaluate_mutation_authority(
        tmp_path, run_id="run-1", attempt=1, task_contract_hash=receipt["task_contract_hash"],
        plan_hash=receipt["plan_hash"], lease_id="lease-1", fencing_token="7",
    )
    assert verdict["ok"] is True, verdict


def test_evaluate_mutation_authority_blocks_on_stale_plan_hash_after_disk_round_trip(tmp_path):
    # Simulates the repo/plan changing after planning: re-verification against the
    # NEW plan hash must invalidate the OLD authority rather than accept it.
    plan_validation = _valid_plan_validation(tmp_path)
    receipt = build_planning_receipt(run_id="run-1", attempt=1, contract=_contract(),
                                     plan=_plan(tmp_path), plan_validation=plan_validation)
    receipt_path(tmp_path).write_text(json.dumps(receipt), encoding="utf-8")

    verdict = evaluate_mutation_authority(
        tmp_path, run_id="run-1", attempt=1, task_contract_hash=receipt["task_contract_hash"],
        plan_hash="a-different-plan-hash-after-drift",
    )
    assert verdict["ok"] is False
    assert verdict["reason_code"] == "mutation_authority_invalid"


def test_evaluate_mutation_authority_blocks_when_receipt_was_never_ready(tmp_path):
    bad_validation = {"valid": False, "errors": ["x"], "warnings": [], "checked_tasks": 1}
    receipt = build_planning_receipt(run_id="run-1", attempt=1, contract=_contract(), plan={},
                                     plan_validation=bad_validation)
    receipt_path(tmp_path).write_text(json.dumps(receipt), encoding="utf-8")

    verdict = evaluate_mutation_authority(tmp_path, run_id="run-1", attempt=1,
                                          task_contract_hash=receipt["task_contract_hash"],
                                          plan_hash=receipt["plan_hash"])
    assert verdict["ok"] is False
    assert verdict["reason_code"] == "planning_not_ready"


def test_evaluate_mutation_authority_blocks_on_corrupt_receipt(tmp_path):
    receipt_path(tmp_path).write_text("{not json", encoding="utf-8")
    verdict = evaluate_mutation_authority(tmp_path, run_id="run-1", attempt=1,
                                          task_contract_hash="c1", plan_hash="p1")
    assert verdict["ok"] is False
    assert verdict["reason_code"] == "planning_receipt_missing"


def test_evaluate_mutation_authority_blocks_on_wrong_schema(tmp_path):
    receipt_path(tmp_path).write_text(json.dumps({"schema": "not-the-right-schema"}), encoding="utf-8")
    verdict = evaluate_mutation_authority(tmp_path, run_id="run-1", attempt=1,
                                          task_contract_hash="c1", plan_hash="p1")
    assert verdict["ok"] is False
    assert verdict["reason_code"] == "planning_receipt_schema_invalid"


# --- #284 item 1: GitHub source-revision capture folded into the identity tuple ----


def _source_snapshot(snapshot_hash: str) -> dict:
    return {
        "schema": "simplicio.source-snapshot/v1",
        "source": {
            "provider": "github", "repo": "acme/repo", "item_id": "284",
            "revision": "2026-01-01T00:00:00Z#comments=0",
            "snapshot_hash": snapshot_hash, "observed_at": "2026-01-01T00:00:00Z",
        },
    }


def test_mutation_authority_token_changes_with_source_snapshot_hash():
    base = dict(run_id="r1", attempt=1, task_contract_hash="c1", plan_hash="p1")
    without_source = mutation_authority_token(**base)
    with_source_a = mutation_authority_token(**base, source_snapshot_hash="hash-a")
    with_source_b = mutation_authority_token(**base, source_snapshot_hash="hash-b")
    assert without_source != with_source_a
    assert with_source_a != with_source_b


def test_build_planning_receipt_embeds_source_snapshot_and_folds_hash_into_authority(tmp_path):
    plan_validation = _valid_plan_validation(tmp_path)
    snapshot = _source_snapshot("hash-a")
    receipt = build_planning_receipt(run_id="run-1", attempt=1, contract=_contract(),
                                     plan=_plan(tmp_path), plan_validation=plan_validation,
                                     source_snapshot=snapshot)
    assert receipt["ready_for_mutation"] is True
    assert receipt["source"]["snapshot_hash"] == "hash-a"
    # the authority must NOT equal the one minted without a source snapshot
    no_source_receipt = build_planning_receipt(run_id="run-1", attempt=1, contract=_contract(),
                                               plan=_plan(tmp_path), plan_validation=plan_validation)
    assert receipt["mutation_authority"] != no_source_receipt["mutation_authority"]


def test_build_planning_receipt_omits_source_block_when_no_snapshot_given(tmp_path):
    plan_validation = _valid_plan_validation(tmp_path)
    receipt = build_planning_receipt(run_id="run-1", attempt=1, contract=_contract(),
                                     plan=_plan(tmp_path), plan_validation=plan_validation)
    assert "source" not in receipt


def test_evaluate_mutation_authority_blocks_on_source_drift(tmp_path):
    plan_validation = _valid_plan_validation(tmp_path)
    receipt = build_planning_receipt(run_id="run-1", attempt=1, contract=_contract(),
                                     plan=_plan(tmp_path), plan_validation=plan_validation,
                                     source_snapshot=_source_snapshot("hash-a"))
    receipt_path(tmp_path).write_text(json.dumps(receipt), encoding="utf-8")

    unchanged = evaluate_mutation_authority(
        tmp_path, run_id="run-1", attempt=1, task_contract_hash=receipt["task_contract_hash"],
        plan_hash=receipt["plan_hash"], source_snapshot_hash="hash-a",
    )
    assert unchanged["ok"] is True, unchanged

    drifted = evaluate_mutation_authority(
        tmp_path, run_id="run-1", attempt=1, task_contract_hash=receipt["task_contract_hash"],
        plan_hash=receipt["plan_hash"], source_snapshot_hash="hash-b-after-issue-edit",
    )
    assert drifted["ok"] is False
    assert drifted["reason_code"] == "source_drift"


def test_evaluate_mutation_authority_ignores_source_hash_when_receipt_has_none(tmp_path):
    # a receipt built WITHOUT a source snapshot (local/non-GitHub run) must not be
    # penalized just because the caller happens to pass a source_snapshot_hash.
    plan_validation = _valid_plan_validation(tmp_path)
    receipt = build_planning_receipt(run_id="run-1", attempt=1, contract=_contract(),
                                     plan=_plan(tmp_path), plan_validation=plan_validation)
    receipt_path(tmp_path).write_text(json.dumps(receipt), encoding="utf-8")

    verdict = evaluate_mutation_authority(
        tmp_path, run_id="run-1", attempt=1, task_contract_hash=receipt["task_contract_hash"],
        plan_hash=receipt["plan_hash"], source_snapshot_hash="irrelevant-because-receipt-has-no-source",
    )
    assert verdict["ok"] is True, verdict
