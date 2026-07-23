import json

import pytest

from simplicio_loop.model_routing_policy import (
    BLOCKED,
    DETERMINISTIC,
    DOWNGRADE,
    LOCAL,
    REMOTE,
    STRONG_LOCAL,
    RoutingPolicyError,
    evaluate,
    maybe_downgrade,
    record_observation,
)


def base(**overrides):
    value = {
        "deterministic_capable": False,
        "local_capable": True,
        "strong_local_capable": True,
        "remote_available": True,
        "remote_allowed": True,
        "remote_provider": "remote-a",
        "allowed_remote_providers": ["remote-a"],
        "max_cost_usd": 1.0,
        "estimated_remote_cost_usd": 0.10,
        "higher_capability_required": False,
    }
    value.update(overrides)
    return value


def test_easy_task_stays_deterministic_and_receipt_is_bounded():
    receipt = evaluate(base(deterministic_capable=True), now=10)
    assert receipt["decision"] == DETERMINISTIC
    assert receipt["remote_request_allowed"] is False
    assert receipt["mutation_authority"] == "runtime"
    assert receipt["mutation_authorized"] is False
    assert receipt["handoff"]["transcript_included"] is False
    assert len(receipt["receipt_id"]) == 24
    json.dumps(receipt)


@pytest.mark.parametrize("field", ["privacy_sensitive", "local_only", "offline"])
def test_safety_constraints_never_permit_remote(field):
    receipt = evaluate(base(**{field: True}, higher_capability_required=True))
    assert receipt["decision"] != REMOTE
    assert receipt["remote_request_allowed"] is False
    assert field in receipt["reason_codes"]


def test_cost_and_provider_gates_fall_back_locally():
    cost = evaluate(base(higher_capability_required=True, estimated_remote_cost_usd=2.0))
    provider = evaluate(base(higher_capability_required=True, allowed_remote_providers=["other"]))
    assert cost["decision"] == STRONG_LOCAL
    assert provider["decision"] == STRONG_LOCAL


def test_stall_escalates_once_when_remote_is_allowed():
    receipt = evaluate(base(stall_detected=True, strong_local_capable=False), now=20)
    assert receipt["decision"] == REMOTE
    assert receipt["escalation_count"] == 1
    assert "measured_escalation" in receipt["reason_codes"]


def test_invalid_syntax_repairs_before_escalating():
    repair = evaluate(base(stall_detected=True, strong_local_capable=False, invalid_tool_syntax=True, syntax_repairs=0), now=20)
    exhausted = evaluate(base(stall_detected=True, strong_local_capable=False, invalid_tool_syntax=True, syntax_repairs=1), now=20)
    assert repair["decision"] == LOCAL
    assert "bounded_tool_syntax_repair" in repair["reason_codes"]
    assert exhausted["decision"] == REMOTE


def test_cooldown_and_budget_cap_prevent_ping_pong():
    cooldown = evaluate(base(stall_detected=True, current_tier=LOCAL, cooldown_until=100), now=20)
    capped = evaluate(base(stall_detected=True, escalation_count=1, max_escalations=1))
    assert cooldown["decision"] == LOCAL
    assert capped["decision"] == STRONG_LOCAL


def test_observation_and_downgrade_preserve_effect_ids():
    receipt = evaluate(base(stall_detected=True, completed_effect_ids=["effect-2", "effect-1"]))
    observed = record_observation(receipt, calls=2, tokens=30, elapsed_seconds=1.5, cost_usd=0.2, outcome="success")
    downgraded = maybe_downgrade(observed)
    assert observed["observed_usage"] == {"calls": 2, "tokens": 30, "elapsed_seconds": 1.5, "cost_usd": 0.2}
    assert downgraded["decision"] == DOWNGRADE
    assert downgraded["remote_request_allowed"] is False
    assert downgraded["completed_effect_ids"] == ["effect-1", "effect-2"]
    assert downgraded["mutation_authorized"] is False


def test_malformed_safety_field_is_rejected():
    with pytest.raises(RoutingPolicyError, match="privacy_sensitive"):
        evaluate(base(privacy_sensitive="false"))


def test_no_safe_fallback_is_blocked():
    receipt = evaluate(base(local_capable=False, strong_local_capable=False, remote_allowed=False))
    assert receipt["decision"] == BLOCKED
    assert "no_safe_fallback" in receipt["reason_codes"]
