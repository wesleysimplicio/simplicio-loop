from __future__ import annotations

import pytest

from simplicio_loop.prototype_gate import (
    DEFAULT_MAX_REVISE,
    LEVELS,
    PrototypeGateError,
    apply_decision,
    build_candidate,
    build_decision,
    build_not_required_receipt,
    build_plan,
    build_receipt,
    classify_necessity,
    gate_status,
    init_state,
    load_state,
    save_state,
    stall_verdict,
    validate_candidate,
    validate_decision,
    validate_plan,
    validate_receipt,
)


def _plan(level="P1", source_sha="abc"):
    return build_plan(work_item_id="wi-568", goal="choose API shape", prototype_type="schema",
                      source_sha=source_sha, level=level,
                      validators=["python -m json.tool schema.json"])


def _candidate(plan, candidate_id="cand-1", artifact_hash="hash-1"):
    return build_candidate(plan=plan, candidate_id=candidate_id, strategy="direct",
                           agent_id="agent-1", artifact_hash=artifact_hash)


# --- existing plan/decision coverage (unchanged behavior) ------------------------------------

def test_plan_is_hash_bound_and_budgeted():
    plan = _plan()
    assert plan["schema"] == "simplicio.prototype-plan/v1"
    assert plan["budget_fraction"] == 0.10
    assert validate_plan(plan, current_source_sha="abc")["valid"] is True


def test_source_drift_fails_closed():
    plan = _plan()
    assert validate_plan(plan, current_source_sha="changed")["valid"] is False
    with pytest.raises(PrototypeGateError, match="source drift"):
        validate_decision(build_decision(plan=plan, candidate_hash="candidate", decision="ACCEPT"),
                          plan=plan, candidate_hash="candidate", current_source_sha="changed")


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


def test_decision_optional_fields_default_empty_but_are_hash_bound():
    plan = _plan()
    decision = build_decision(plan=plan, candidate_hash="candidate", decision="ACCEPT")
    assert decision["judge_id"] == ""
    assert decision["judge_independent"] is True
    assert decision["ranked_candidates"] == []
    with_judge = build_decision(plan=plan, candidate_hash="candidate", decision="ACCEPT",
                                judge_id="judge-1", judge_independent=False,
                                allowed_next_stage="P1")
    assert with_judge["decision_hash"] != decision["decision_hash"]
    assert validate_decision(with_judge, plan=plan, candidate_hash="candidate")["allowed_next_stage"] == "P1"


def test_build_decision_rejects_bad_allowed_next_stage():
    plan = _plan()
    with pytest.raises(PrototypeGateError, match="allowed_next_stage"):
        build_decision(plan=plan, candidate_hash="candidate", decision="ACCEPT", allowed_next_stage="P9")


# --- candidate schema (simplicio.prototype-candidate/v1) ------------------------------------

def test_candidate_is_hash_bound_and_plan_bound():
    plan = _plan()
    candidate = _candidate(plan)
    assert candidate["schema"] == "simplicio.prototype-candidate/v1"
    result = validate_candidate(candidate, plan=plan)
    assert result["valid"] is True
    assert result["plan_bound"] is True


def test_candidate_rejects_unbound_plan():
    plan_a = _plan(source_sha="a")
    plan_b = _plan(source_sha="b")
    candidate = _candidate(plan_a)
    result = validate_candidate(candidate, plan=plan_b)
    assert result["valid"] is False
    assert result["reason_code"] == "plan_mismatch"


def test_candidate_tampering_is_detected():
    plan = _plan()
    candidate = _candidate(plan)
    tampered = dict(candidate, artifact_hash="different-hash")
    with pytest.raises(PrototypeGateError, match="hash mismatch"):
        validate_candidate(tampered, plan=plan)


def test_candidate_rejects_unknown_status():
    plan = _plan()
    with pytest.raises(PrototypeGateError, match="unsupported candidate status"):
        build_candidate(plan=plan, candidate_id="c", strategy="s", agent_id="a",
                        artifact_hash="h", status="not-a-status")


def test_candidate_requires_core_identity_fields():
    plan = _plan()
    with pytest.raises(PrototypeGateError):
        build_candidate(plan=plan, candidate_id="", strategy="s", agent_id="a", artifact_hash="h")
    with pytest.raises(PrototypeGateError, match="artifact_hash"):
        build_candidate(plan=plan, candidate_id="c", strategy="s", agent_id="a", artifact_hash="")


# --- receipt schema (simplicio.prototype-receipt/v1) -----------------------------------------

def test_receipt_chains_plan_candidate_decision_by_hash():
    plan = _plan()
    candidate = _candidate(plan)
    decision = build_decision(plan=plan, candidate_hash=candidate["candidate_hash"], decision="ACCEPT")
    receipt = build_receipt(plan=plan, candidate=candidate, decision=decision,
                            stage_hashes={"hypothesis": "h1", "tests": "h2"}, attempt=2, fence="F1")
    assert receipt["schema"] == "simplicio.prototype-receipt/v1"
    assert receipt["candidate_hash"] == candidate["candidate_hash"]
    assert receipt["decision_hash"] == decision["decision_hash"]
    assert receipt["attempt"] == 2
    result = validate_receipt(receipt, plan=plan, candidate=candidate, decision=decision)
    assert result["valid"] is True


def test_receipt_requires_accept_decision():
    plan = _plan()
    candidate = _candidate(plan)
    decision = build_decision(plan=plan, candidate_hash=candidate["candidate_hash"], decision="REVISE")
    with pytest.raises(PrototypeGateError, match="not ACCEPT"):
        build_receipt(plan=plan, candidate=candidate, decision=decision)


def test_receipt_rejects_unknown_stage():
    plan = _plan()
    candidate = _candidate(plan)
    decision = build_decision(plan=plan, candidate_hash=candidate["candidate_hash"], decision="ACCEPT")
    with pytest.raises(PrototypeGateError, match="unknown receipt stage"):
        build_receipt(plan=plan, candidate=candidate, decision=decision, stage_hashes={"bogus": "x"})


def test_receipt_binding_mismatch_is_flagged_not_silently_ignored():
    plan = _plan()
    candidate = _candidate(plan)
    other_candidate = _candidate(plan, candidate_id="cand-2", artifact_hash="hash-2")
    decision = build_decision(plan=plan, candidate_hash=candidate["candidate_hash"], decision="ACCEPT")
    receipt = build_receipt(plan=plan, candidate=candidate, decision=decision)
    result = validate_receipt(receipt, candidate=other_candidate)
    assert result["valid"] is False
    assert result["reason_code"] == "candidate_mismatch"


# --- prototype-necessity classifier -----------------------------------------------------------

def test_classifier_trivial_change_needs_no_prototype():
    result = classify_necessity(task_description="fix a typo in a docstring", signals={})
    assert result["required"] is False
    assert result["level"] is None
    assert result["rules_fired"] == []


def test_classifier_is_explainable_and_picks_highest_level():
    result = classify_necessity(task_description="new external API + UI change", signals={
        "api": True, "ui": True,
    })
    assert result["required"] is True
    assert result["level"] == "P2"
    fired_rules = {r["rule"] for r in result["rules_fired"]}
    assert "architecture_or_multi_repo_or_api" in fired_rules
    assert "ui_only" in fired_rules  # every matching rule is surfaced, not just the winner


def test_classifier_security_or_explicit_request_forces_full():
    assert classify_necessity(task_description="t", signals={"security": True})["level"] == "FULL"
    assert classify_necessity(task_description="t", signals={"explicit_human_request": True})["level"] == "FULL"


def test_classifier_rejects_unknown_signal():
    with pytest.raises(PrototypeGateError, match="unknown risk signal"):
        classify_necessity(task_description="t", signals={"not_a_real_signal": True})


def test_not_required_receipt_refuses_when_prototype_is_actually_required():
    with pytest.raises(PrototypeGateError, match="cannot emit prototype_not_required"):
        build_not_required_receipt(work_item_id="wi-1", task_description="t",
                                   signals={"security": True})


def test_not_required_receipt_for_trivial_work():
    receipt = build_not_required_receipt(work_item_id="wi-1", task_description="fix a typo",
                                         estimate={"minutes": 5}, policy="trivial-docs")
    assert receipt["schema"] == "simplicio.prototype-not-required/v1"
    assert receipt["estimate"] == {"minutes": 5}


# --- promotion state machine: P0 -> P1 -> P2 -> FULL, bounded REVISE, drift -------------------

def _accept(plan, candidate):
    return build_decision(plan=plan, candidate_hash=candidate["candidate_hash"], decision="ACCEPT")


def _revise(plan, candidate, reason="needs more evidence"):
    return build_decision(plan=plan, candidate_hash=candidate["candidate_hash"], decision="REVISE",
                          reason=reason)


def test_promotion_walks_p0_through_full_in_order():
    plan = _plan(level="P0")
    candidate = _candidate(plan)
    state = init_state(work_item_id="wi-promo", plan=plan)
    assert state["current_level"] == "P0"
    seen_levels = [state["current_level"]]
    for _ in range(len(LEVELS) - 1):
        state = apply_decision(state, plan=plan, decision=_accept(plan, candidate),
                               candidate_hash=candidate["candidate_hash"])
        seen_levels.append(state["current_level"])
    assert seen_levels == list(LEVELS)
    assert state["status"] == "in_progress"
    # one more ACCEPT at FULL resolves the flow -- never before FULL is reached
    state = apply_decision(state, plan=plan, decision=_accept(plan, candidate),
                           candidate_hash=candidate["candidate_hash"])
    assert state["status"] == "resolved"
    assert state["current_level"] == "FULL"


def test_accept_never_marks_done_before_full():
    plan = _plan(level="P0")
    candidate = _candidate(plan)
    state = init_state(work_item_id="wi-1", plan=plan)
    state = apply_decision(state, plan=plan, decision=_accept(plan, candidate),
                           candidate_hash=candidate["candidate_hash"])
    assert state["status"] == "in_progress"
    assert state["current_level"] == "P1"


def test_revise_is_bounded_and_then_blocks():
    plan = _plan(level="P0")
    candidate = _candidate(plan)
    state = init_state(work_item_id="wi-revise", plan=plan)
    for _ in range(DEFAULT_MAX_REVISE):
        state = apply_decision(state, plan=plan, decision=_revise(plan, candidate),
                               candidate_hash=candidate["candidate_hash"])
        assert state["status"] == "in_progress"
    # one more REVISE past the bound blocks the flow
    state = apply_decision(state, plan=plan, decision=_revise(plan, candidate),
                           candidate_hash=candidate["candidate_hash"])
    assert state["status"] == "blocked"
    assert state["blocked_reason"] == "revise_iterations_exceeded"


def test_reject_and_blocked_are_terminal_with_recorded_reason():
    plan = _plan(level="P0")
    candidate = _candidate(plan)
    state = init_state(work_item_id="wi-reject", plan=plan)
    decision = build_decision(plan=plan, candidate_hash=candidate["candidate_hash"], decision="REJECT",
                              reason="not viable")
    state = apply_decision(state, plan=plan, decision=decision, candidate_hash=candidate["candidate_hash"])
    assert state["status"] == "rejected"
    assert state["history"][-1]["reason"] == "not viable"
    with pytest.raises(PrototypeGateError, match="terminal"):
        apply_decision(state, plan=plan, decision=_accept(plan, candidate),
                       candidate_hash=candidate["candidate_hash"])


def test_plan_or_source_drift_invalidates_the_state_machine():
    plan = _plan(level="P0", source_sha="rev-1")
    candidate = _candidate(plan)
    state = init_state(work_item_id="wi-drift", plan=plan)
    state = apply_decision(state, plan=plan, decision=_accept(plan, candidate),
                           candidate_hash=candidate["candidate_hash"], current_source_sha="rev-2")
    assert state["status"] == "blocked"
    assert state["blocked_reason"] == "source_drift"


def test_decision_bound_to_wrong_candidate_is_rejected():
    plan = _plan(level="P0")
    candidate = _candidate(plan)
    other = _candidate(plan, candidate_id="other", artifact_hash="other-hash")
    state = init_state(work_item_id="wi-x", plan=plan)
    decision = _revise(plan, other)
    with pytest.raises(PrototypeGateError, match="stale or not bound"):
        apply_decision(state, plan=plan, decision=decision, candidate_hash=candidate["candidate_hash"])


def test_stall_verdict_reuses_loop_journal_and_detects_oscillation():
    plan = _plan(level="P0")
    candidate = _candidate(plan)
    state = init_state(work_item_id="wi-stall", plan=plan)
    for _ in range(3):
        state = apply_decision(state, plan=plan, decision=_revise(plan, candidate, reason="same reason every time"),
                               candidate_hash=candidate["candidate_hash"])
    verdict = stall_verdict(state, k=3)
    assert verdict["verdict"] == "STALLED"


def test_stall_verdict_is_progress_when_reasons_differ():
    plan = _plan(level="P0")
    candidate = _candidate(plan)
    state = init_state(work_item_id="wi-progress", plan=plan)
    state = apply_decision(state, plan=plan, decision=_revise(plan, candidate, reason="reason A"),
                           candidate_hash=candidate["candidate_hash"])
    verdict = stall_verdict(state, k=3)
    assert verdict["verdict"] == "PROGRESS"


# --- file persistence + task-anchor integration point -----------------------------------------

def test_save_load_state_round_trips(tmp_path):
    plan = _plan(level="P0")
    state = init_state(work_item_id="wi-persist", plan=plan)
    path = save_state(state, repo=str(tmp_path))
    assert path.endswith("wi-persist.json")
    loaded = load_state("wi-persist", repo=str(tmp_path))
    assert loaded == state


def test_gate_status_untracked_item_is_ready():
    status = gate_status("no-such-item-anywhere", repo="/tmp")
    assert status == {"tracked": False, "ready": True,
                      "reason": "no prototype flow tracked for this item"}


def test_gate_status_blocks_while_in_progress_and_unblocks_when_resolved(tmp_path):
    plan = _plan(level="FULL")
    candidate = _candidate(plan)
    state = init_state(work_item_id="wi-gate", plan=plan)
    save_state(state, repo=str(tmp_path))
    status = gate_status("wi-gate", repo=str(tmp_path))
    assert status["tracked"] is True
    assert status["ready"] is False

    resolved = apply_decision(state, plan=plan, decision=_accept(plan, candidate),
                              candidate_hash=candidate["candidate_hash"])
    assert resolved["status"] == "resolved"
    save_state(resolved, repo=str(tmp_path))
    status = gate_status("wi-gate", repo=str(tmp_path))
    assert status["ready"] is True
