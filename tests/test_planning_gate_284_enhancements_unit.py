"""Tests for new #284 planning_gate capabilities.

Covers:
- source_revision is included in the authority token (STALE_SOURCE)
- awaiting_decision → AWAITING_DECISION verdict, ready_for_mutation=False
- authority_ttl_seconds → authority_expires_at populated
- evaluate_mutation_authority returns STALE_SOURCE on current_source_revision mismatch
- evaluate_mutation_authority returns LEASE_LOST on lease/fencing mismatch
- evaluate_mutation_authority returns AWAITING_DECISION when receipt has flag
- evaluate_mutation_authority returns AUTHORITY_EXPIRED when TTL elapsed
- evaluate_mutation_authority returns VERDICT_COMPLETE on full happy path
- verdict field present in all evaluate responses
- New verdict constants exported
"""
import json
import time

import pytest

from simplicio_loop.plan_contract import PLAN_SCHEMA, validate_plan
from simplicio_loop.planning_gate import (
    DEFAULT_AUTHORITY_TTL_SECONDS,
    PLANNING_RECEIPT_SCHEMA,
    VERDICT_AUTHORITY_EXPIRED,
    VERDICT_AWAITING_DECISION,
    VERDICT_BLOCKED,
    VERDICT_COMPLETE,
    VERDICT_LEASE_LOST,
    VERDICT_STALE_SOURCE,
    build_planning_receipt,
    content_hash,
    evaluate_mutation_authority,
    mutation_authority_token,
    receipt_path,
    verify_mutation_authority,
)


# ---------------------------------------------------------------------------
# Helpers (same as test_planning_gate_unit.py — duplicated for clarity)
# ---------------------------------------------------------------------------

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
        "steps": [{"candidate_targets": ["src/app.py"], "to_create": ["src/app.py"], "rule_ids": [],
                   "steps": [{"scenario_id": "S1", "plan": {
                       "read_paths": ["src/app.py"], "change_paths": ["src/app.py"],
                       "test_commands": ["pytest"],
                   }}]}],
    }


def _valid_plan_validation(tmp_path):
    return validate_plan(_plan(tmp_path), _contract()["tasks"], tmp_path,
                         contract_hash="contract-1",
                         current_state={"head": "head-1", "tree_hash": "tree-1"})


def _write_receipt(tmp_path, receipt):
    receipt_path(tmp_path).write_text(json.dumps(receipt), encoding="utf-8")
    return receipt


def _build_and_write(tmp_path, **kwargs):
    validation = _valid_plan_validation(tmp_path)
    receipt = build_planning_receipt(
        run_id="run-1", attempt=1, contract=_contract(),
        plan=_plan(tmp_path), plan_validation=validation,
        **kwargs,
    )
    _write_receipt(tmp_path, receipt)
    return receipt


# ---------------------------------------------------------------------------
# source_revision in mutation_authority_token
# ---------------------------------------------------------------------------

def test_source_revision_changes_the_token():
    base = dict(run_id="r1", attempt=1, task_contract_hash="c1", plan_hash="p1")
    t1 = mutation_authority_token(**base, source_revision="rev-A")
    t2 = mutation_authority_token(**base, source_revision="rev-B")
    assert t1 != t2


def test_source_revision_empty_is_distinct_from_non_empty():
    base = dict(run_id="r1", attempt=1, task_contract_hash="c1", plan_hash="p1")
    t_empty = mutation_authority_token(**base, source_revision="")
    t_set = mutation_authority_token(**base, source_revision="rev-X")
    assert t_empty != t_set


def test_verify_mutation_authority_includes_source_revision():
    token = mutation_authority_token(run_id="r", attempt=1, task_contract_hash="c",
                                      plan_hash="p", source_revision="rev-1")
    assert verify_mutation_authority(token, run_id="r", attempt=1, task_contract_hash="c",
                                      plan_hash="p", source_revision="rev-1")
    assert not verify_mutation_authority(token, run_id="r", attempt=1, task_contract_hash="c",
                                          plan_hash="p", source_revision="rev-CHANGED")


# ---------------------------------------------------------------------------
# build_planning_receipt — awaiting_decision
# ---------------------------------------------------------------------------

def test_build_planning_receipt_awaiting_decision_blocks(tmp_path):
    validation = _valid_plan_validation(tmp_path)
    receipt = build_planning_receipt(
        run_id="run-1", attempt=1, contract=_contract(),
        plan=_plan(tmp_path), plan_validation=validation,
        awaiting_decision=True, awaiting_reason="scope ambiguous",
    )
    assert receipt["ready_for_mutation"] is False
    assert receipt["verdict"] == VERDICT_AWAITING_DECISION
    assert receipt["mutation_authority"] == ""
    assert "scope ambiguous" in receipt["awaiting_reason"]


def test_build_planning_receipt_awaiting_decision_overrides_valid_plan(tmp_path):
    """Even a perfectly valid plan MUST yield AWAITING_DECISION when flag is set."""
    validation = _valid_plan_validation(tmp_path)
    assert validation["valid"] is True
    receipt = build_planning_receipt(
        run_id="run-1", attempt=1, contract=_contract(),
        plan=_plan(tmp_path), plan_validation=validation,
        awaiting_decision=True,
    )
    assert receipt["verdict"] == VERDICT_AWAITING_DECISION
    assert receipt["ready_for_mutation"] is False


# ---------------------------------------------------------------------------
# build_planning_receipt — authority TTL
# ---------------------------------------------------------------------------

def test_build_planning_receipt_has_expires_at_by_default(tmp_path):
    receipt = _build_and_write(tmp_path)
    assert receipt["authority_expires_at"] != ""


def test_build_planning_receipt_ttl_zero_omits_expires_at(tmp_path):
    validation = _valid_plan_validation(tmp_path)
    receipt = build_planning_receipt(
        run_id="run-1", attempt=1, contract=_contract(),
        plan=_plan(tmp_path), plan_validation=validation,
        authority_ttl_seconds=0,
    )
    assert receipt["authority_expires_at"] == ""


def test_build_planning_receipt_source_revision_in_receipt(tmp_path):
    receipt = _build_and_write(tmp_path, source_revision="rev-42")
    assert receipt["source_revision"] == "rev-42"


# ---------------------------------------------------------------------------
# evaluate_mutation_authority — STALE_SOURCE
# ---------------------------------------------------------------------------

def test_evaluate_stale_source_when_revision_changed(tmp_path):
    receipt = _build_and_write(tmp_path, source_revision="rev-planning")
    verdict = evaluate_mutation_authority(
        tmp_path, run_id="run-1", attempt=1,
        task_contract_hash=receipt["task_contract_hash"],
        plan_hash=receipt["plan_hash"],
        source_revision="rev-planning",
        current_source_revision="rev-CHANGED",
    )
    assert verdict["ok"] is False
    assert verdict["verdict"] == VERDICT_STALE_SOURCE
    assert verdict["reason_code"] == "source_revision_changed"


def test_evaluate_no_stale_source_when_revision_unchanged(tmp_path):
    receipt = _build_and_write(tmp_path, source_revision="rev-planning")
    verdict = evaluate_mutation_authority(
        tmp_path, run_id="run-1", attempt=1,
        task_contract_hash=receipt["task_contract_hash"],
        plan_hash=receipt["plan_hash"],
        source_revision="rev-planning",
        current_source_revision="rev-planning",
    )
    assert verdict["ok"] is True
    assert verdict["verdict"] == VERDICT_COMPLETE


def test_evaluate_no_stale_source_when_current_revision_omitted(tmp_path):
    """Not passing current_source_revision = caller doesn't want source drift check."""
    receipt = _build_and_write(tmp_path, source_revision="rev-planning")
    verdict = evaluate_mutation_authority(
        tmp_path, run_id="run-1", attempt=1,
        task_contract_hash=receipt["task_contract_hash"],
        plan_hash=receipt["plan_hash"],
        source_revision="rev-planning",
    )
    assert verdict["ok"] is True


# ---------------------------------------------------------------------------
# evaluate_mutation_authority — LEASE_LOST
# ---------------------------------------------------------------------------

def test_evaluate_lease_lost_when_lease_id_changed(tmp_path):
    receipt = _build_and_write(tmp_path, lease_id="lease-A", fencing_token="1",
                                authority_ttl_seconds=0)
    verdict = evaluate_mutation_authority(
        tmp_path, run_id="run-1", attempt=1,
        task_contract_hash=receipt["task_contract_hash"],
        plan_hash=receipt["plan_hash"],
        lease_id="lease-B",        # ← rotated
        fencing_token="1",
        source_revision="",
    )
    assert verdict["ok"] is False
    assert verdict["verdict"] == VERDICT_LEASE_LOST
    assert verdict["reason_code"] == "lease_or_fence_mismatch"


def test_evaluate_lease_lost_when_fencing_token_changed(tmp_path):
    receipt = _build_and_write(tmp_path, lease_id="lease-A", fencing_token="1",
                                authority_ttl_seconds=0)
    verdict = evaluate_mutation_authority(
        tmp_path, run_id="run-1", attempt=1,
        task_contract_hash=receipt["task_contract_hash"],
        plan_hash=receipt["plan_hash"],
        lease_id="lease-A",
        fencing_token="999",       # ← rotated
        source_revision="",
    )
    assert verdict["ok"] is False
    assert verdict["verdict"] == VERDICT_LEASE_LOST


# ---------------------------------------------------------------------------
# evaluate_mutation_authority — AWAITING_DECISION
# ---------------------------------------------------------------------------

def test_evaluate_awaiting_decision_from_receipt(tmp_path):
    validation = _valid_plan_validation(tmp_path)
    receipt = build_planning_receipt(
        run_id="run-1", attempt=1, contract=_contract(),
        plan=_plan(tmp_path), plan_validation=validation,
        awaiting_decision=True, awaiting_reason="need human input",
    )
    _write_receipt(tmp_path, receipt)
    verdict = evaluate_mutation_authority(
        tmp_path, run_id="run-1", attempt=1,
        task_contract_hash=receipt["task_contract_hash"],
        plan_hash=receipt["plan_hash"],
    )
    assert verdict["ok"] is False
    assert verdict["verdict"] == VERDICT_AWAITING_DECISION
    assert "human input" in verdict["reason"]


# ---------------------------------------------------------------------------
# evaluate_mutation_authority — AUTHORITY_EXPIRED
# ---------------------------------------------------------------------------

def test_evaluate_authority_expired_when_ttl_elapsed(tmp_path):
    validation = _valid_plan_validation(tmp_path)
    receipt = build_planning_receipt(
        run_id="run-1", attempt=1, contract=_contract(),
        plan=_plan(tmp_path), plan_validation=validation,
        authority_ttl_seconds=1,  # 1 second TTL
    )
    # Manually backdated expires_at
    past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    receipt["authority_expires_at"] = past
    _write_receipt(tmp_path, receipt)
    verdict = evaluate_mutation_authority(
        tmp_path, run_id="run-1", attempt=1,
        task_contract_hash=receipt["task_contract_hash"],
        plan_hash=receipt["plan_hash"],
    )
    assert verdict["ok"] is False
    assert verdict["verdict"] == VERDICT_AUTHORITY_EXPIRED
    assert verdict["reason_code"] == "authority_expired"


def test_evaluate_authority_not_expired_when_future(tmp_path):
    receipt = _build_and_write(tmp_path, authority_ttl_seconds=3600)
    verdict = evaluate_mutation_authority(
        tmp_path, run_id="run-1", attempt=1,
        task_contract_hash=receipt["task_contract_hash"],
        plan_hash=receipt["plan_hash"],
    )
    assert verdict["ok"] is True
    assert verdict["verdict"] == VERDICT_COMPLETE


# ---------------------------------------------------------------------------
# verdict field always present
# ---------------------------------------------------------------------------

def test_evaluate_verdict_field_present_on_missing_receipt(tmp_path):
    verdict = evaluate_mutation_authority(
        tmp_path, run_id="run-1", attempt=1, task_contract_hash="c", plan_hash="p")
    assert "verdict" in verdict
    assert verdict["verdict"] == VERDICT_BLOCKED


def test_evaluate_verdict_field_present_on_schema_mismatch(tmp_path):
    receipt_path(tmp_path).write_text(json.dumps({"schema": "bad"}), encoding="utf-8")
    verdict = evaluate_mutation_authority(
        tmp_path, run_id="run-1", attempt=1, task_contract_hash="c", plan_hash="p")
    assert "verdict" in verdict


def test_evaluate_verdict_field_present_on_success(tmp_path):
    receipt = _build_and_write(tmp_path, authority_ttl_seconds=0)
    verdict = evaluate_mutation_authority(
        tmp_path, run_id="run-1", attempt=1,
        task_contract_hash=receipt["task_contract_hash"],
        plan_hash=receipt["plan_hash"],
    )
    assert "verdict" in verdict
    assert verdict["verdict"] == VERDICT_COMPLETE


# ---------------------------------------------------------------------------
# Verdict constant exports
# ---------------------------------------------------------------------------

def test_verdict_constants_exported():
    for v in [VERDICT_COMPLETE, VERDICT_BLOCKED, VERDICT_STALE_SOURCE,
              VERDICT_LEASE_LOST, VERDICT_AWAITING_DECISION, VERDICT_AUTHORITY_EXPIRED]:
        assert isinstance(v, str)


def test_default_ttl_positive():
    assert DEFAULT_AUTHORITY_TTL_SECONDS > 0
