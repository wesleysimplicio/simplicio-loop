import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simplicio_loop.flow_semantics import evaluate_converge, evaluate_drain


def test_converge_no_change_never_completes():
    result = evaluate_converge([{"verified": False, "changed": False, "fingerprint": "x"}])
    assert result["status"] == "RETRY"
    assert result["reason_code"] == "no_progress"


def test_converge_repeated_fingerprint_escalates_without_strategy_change():
    attempts = [{"verified": False, "fingerprint": "x", "strategy": "a"}] * 3
    result = evaluate_converge(attempts, max_attempts=5, stall_threshold=3)
    assert result["status"] == "ESCALATE"
    assert result["reason_code"] == "stall_escalation"


def test_converge_repeated_fingerprint_allows_new_strategy_retry():
    attempts = [
        {"verified": False, "fingerprint": "x", "strategy": "a"},
        {"verified": False, "fingerprint": "x", "strategy": "a"},
        {"verified": False, "fingerprint": "x", "strategy": "b"},
    ]
    result = evaluate_converge(attempts, max_attempts=5, stall_threshold=3)
    assert result["status"] == "RETRY"
    assert result["reason_code"] == "strategy_changed"


def test_drain_quarantines_blocked_items():
    result = evaluate_drain([
        {"ready": [], "active": [], "blocked": [{"id": "B", "reason": "dependency", "dead_ends": ["A"]}]},
        {"ready": [], "active": []},
    ])
    assert result["status"] == "DRAINED"
    assert result["quarantined"][0]["id"] == "B"


def test_drain_detects_late_arrival_after_empty_poll():
    result = evaluate_drain([{"ready": [], "active": []}, {"ready": ["late"], "active": []}], k=2)
    assert result["status"] == "CONTINUE"
    assert result["reason_code"] == "late_arrival"
    assert result["late_arrivals"] == ["late"]


def test_drain_requires_k_empty_idle_rounds():
    assert evaluate_drain([{"ready": [], "active": []}], k=2)["status"] == "CONTINUE"
    assert evaluate_drain([{"ready": [], "active": []}, {"ready": [], "active": []}], k=2)["status"] == "DRAINED"
