from __future__ import annotations

import pytest

from simplicio_loop.prototype_gate import (
    PrototypeGateError,
    build_decision,
    build_plan,
    validate_decision,
    validate_plan,
)


def _plan():
    return build_plan(work_item_id="wi-568", goal="choose API shape", prototype_type="schema", source_sha="abc", validators=["python -m json.tool schema.json"])


def test_plan_is_hash_bound_and_budgeted():
    plan = _plan()
    assert plan["schema"] == "simplicio.prototype-plan/v1"
    assert plan["budget_fraction"] == 0.10
    assert validate_plan(plan, current_source_sha="abc")["valid"] is True


def test_source_drift_fails_closed():
    plan = _plan()
    assert validate_plan(plan, current_source_sha="changed")["valid"] is False
    with pytest.raises(PrototypeGateError, match="source drift"):
        validate_decision(build_decision(plan=plan, candidate_hash="candidate", decision="ACCEPT"), plan=plan, candidate_hash="candidate", current_source_sha="changed")


def test_forged_and_stale_decisions_are_rejected():
    plan = _plan()
    decision = build_decision(plan=plan, candidate_hash="candidate", decision="ACCEPT")
    forged = dict(decision, candidate_hash="other")
    with pytest.raises(PrototypeGateError):
        validate_decision(forged, plan=plan, candidate_hash="candidate")
    assert validate_decision(decision, plan=plan, candidate_hash="candidate")["decision"] == "ACCEPT"


def test_non_accept_decision_cannot_promote():
    plan = _plan()
    decision = build_decision(plan=plan, candidate_hash="candidate", decision="REVISE")
    with pytest.raises(PrototypeGateError, match="not ACCEPT"):
        validate_decision(decision, plan=plan, candidate_hash="candidate")
