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


# -- converge: invalid input never masquerades as progress -------------------

def test_converge_rejects_non_sequence_attempts():
    result = evaluate_converge("not-a-sequence")
    assert result["status"] == "BLOCKED"
    assert result["reason_code"] == "attempts_invalid"


def test_converge_rejects_non_mapping_rows():
    result = evaluate_converge([{"verified": False}, "oops"])
    assert result["status"] == "BLOCKED"
    assert result["reason_code"] == "attempt_invalid"


def test_converge_rejects_invalid_limits():
    assert evaluate_converge([], max_attempts=0)["reason_code"] == "limits_invalid"
    assert evaluate_converge([], stall_threshold=0)["reason_code"] == "limits_invalid"


def test_converge_empty_attempts_continues_without_claiming_progress():
    result = evaluate_converge([])
    assert result["status"] == "CONTINUE"
    assert result["reason_code"] == "no_attempts"
    assert result["attempt_count"] == 0


def test_converge_verified_completes_even_on_first_attempt():
    result = evaluate_converge([{"verified": True}])
    assert result["status"] == "COMPLETE"
    assert result["reason_code"] == "verified"
    assert result["strategy_changed"] is False


def test_converge_hits_attempt_cap_before_verification():
    attempts = [{"verified": False, "fingerprint": "x"}] * 2
    result = evaluate_converge(attempts, max_attempts=2, stall_threshold=10)
    assert result["status"] == "ESCALATE"
    assert result["reason_code"] == "attempt_cap"
    assert result["attempt_count"] == 2


def test_converge_changed_but_unverified_reports_attempt_failed():
    result = evaluate_converge(
        [{"verified": False, "fingerprint": "x", "changed": True}], max_attempts=5, stall_threshold=5,
    )
    assert result["status"] == "RETRY"
    assert result["reason_code"] == "attempt_failed"


def test_converge_detects_strategy_change_between_consecutive_attempts():
    attempts = [
        {"verified": False, "fingerprint": "x", "strategy_id": "a"},
        {"verified": False, "fingerprint": "y", "strategy_id": "b"},
    ]
    result = evaluate_converge(attempts, max_attempts=5, stall_threshold=5)
    assert result["strategy_changed"] is True


# -- drain: invalid input never masquerades as drained -----------------------

def test_drain_rejects_non_sequence_rounds():
    result = evaluate_drain("not-a-sequence")
    assert result["status"] == "BLOCKED"
    assert result["reason_code"] == "rounds_invalid"


def test_drain_rejects_invalid_k():
    result = evaluate_drain([], k=0)
    assert result["status"] == "BLOCKED"
    assert result["reason_code"] == "limit_invalid"


def test_drain_rejects_non_mapping_rounds():
    result = evaluate_drain([{"ready": [], "active": []}, "oops"])
    assert result["status"] == "BLOCKED"
    assert result["reason_code"] == "round_invalid"


def test_drain_accepts_a_single_blocked_mapping_not_wrapped_in_a_list():
    result = evaluate_drain([{"ready": [], "active": [], "blocked": {"id": "B", "reason": "dep"}}])
    assert result["quarantined"][0]["id"] == "B"


def test_drain_active_items_keep_the_round_non_empty():
    result = evaluate_drain([{"ready": [], "active": ["still-running"]}], k=1)
    assert result["status"] == "CONTINUE"
    assert result["reason_code"] == "source_not_quiet"
    assert result["empty_rounds"] == 0


def test_drain_quarantine_list_is_sorted_and_deduplicated_by_content():
    result = evaluate_drain([
        {"ready": [], "active": [], "blocked": [
            {"id": "Z", "reason": "dependency"},
            {"id": "A", "reason": "dependency"},
        ]},
        {"ready": [], "active": []},
    ], k=2)
    assert result["status"] == "DRAINED"
    assert [item["id"] for item in result["quarantined"]] == ["A", "Z"]
