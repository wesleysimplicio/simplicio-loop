import pytest

from simplicio_loop.semantic_convergence import (
    ControllerState,
    ConvergenceController,
    EvidenceSnapshot,
    SignalError,
    TransitionError,
    normalize_signal,
)


def signal(number, semantic=None, **extra):
    return {
        "schema": "simplicio.progress-signal/v1",
        "signal_id": f"sig-{number}",
        "source": "agent",
        "evidence": {"id": f"e-{number}", "hash": f"hash-{number}"},
        "semantic": semantic or {"task_frontier": number},
        **extra,
    }


def test_normalize_requires_typed_signal_and_evidence():
    with pytest.raises(SignalError):
        normalize_signal({"schema": "simplicio.progress-signal/v1", "message": "done"})
    normalized = normalize_signal(signal(1))
    assert normalized.source == "agent"
    assert normalized.evidence_id == "e-1"


def test_cosmetic_change_does_not_reset_stall_window():
    controller = ConvergenceController()
    assert controller.step(signal(1))["state"] == "PROGRESSING"
    stalled = controller.step({**signal(2, {"task_frontier": 1}), "prose": "different words"})
    assert stalled["state"] == "STALLED"
    assert stalled["semantic_changed"] is False
    assert stalled["stall_window"] == 1


def test_replan_requires_strategy_delta_and_bounded_route():
    controller = ConvergenceController()
    controller.step(signal(1))
    controller.step(signal(2, {"task_frontier": 1}))
    same = controller.step({**signal(3, {"task_frontier": 1}), "action": "replan", "strategy_hash": "same"})
    assert same["state"] == "REPLAN"
    rejected = controller.step({**signal(4, {"task_frontier": 1}), "action": "replan", "strategy_hash": "same"})
    assert rejected["state"] == "REPLAN"
    assert rejected["reason"] == "awaiting_reroute"
    assert controller.step({**signal(5), "action": "reroute"})["state"] == "REROUTE"
    assert controller.step({**signal(6), "action": "escalate"})["state"] == "ESCALATE"
    assert controller.step({**signal(7), "action": "drain"})["state"] == "DRAIN"


def test_waiting_requires_condition_heartbeat_deadline():
    controller = ConvergenceController()
    waiting = controller.step({**signal(1), "outcome": "waiting", "wait": {"condition": "upstream", "heartbeat": 10, "deadline": 20}}, now=15)
    assert waiting["state"] == "WAITING"
    expired = controller.step({**signal(2), "outcome": "waiting", "wait": {"condition": "upstream", "heartbeat": 10, "deadline": 20}}, now=21)
    assert expired["state"] == "BLOCKED"


def test_verified_requires_delivery_evidence():
    controller = ConvergenceController()
    pending = controller.step({**signal(1), "outcome": "verified"})
    assert pending["state"] == "STALLED"
    verified = controller.step({**signal(2), "outcome": "verified"}, {"acceptance_verified": True, "delivery_verified": True})
    assert verified["state"] == "VERIFIED"


def test_attempt_cap_ends_blocked_and_receipts_are_evidenced():
    controller = ConvergenceController(max_attempts=1)
    controller.step(signal(1))
    blocked = controller.step(signal(2, {"task_frontier": 1}))
    assert blocked["state"] == "BLOCKED"
    assert blocked["reason"] == "attempt_cap_exhausted"
    assert blocked["transition"]["evidence_hash"] == "hash-2"


def test_invalid_transition_is_rejected():
    evidence = EvidenceSnapshot("e-1", "h-1", {})
    with pytest.raises(TransitionError):
        ConvergenceController().transition(ControllerState.REROUTE, evidence, "bad")
