import pytest

from simplicio_loop.token_budget import (
    TOKEN_BUDGET_RECEIPT_SCHEMA,
    TOKEN_BUDGET_SCHEMA,
    TokenBudget,
    TokenBudgetError,
    validate_receipt,
)


def test_budget_contract_is_deterministic_and_preserves_null_metrics():
    budget = TokenBudget("run-760", total_tokens=1000, per_attempt_tokens=400)
    value = budget.as_dict()
    assert value["schema"] == TOKEN_BUDGET_SCHEMA
    assert value["budget_key"] == budget.as_dict()["budget_key"]
    assert value["metrics"]["input_tokens"] is None
    assert value["metrics"]["retry_delta_bytes"] is None


def test_receipt_reports_spent_remaining_and_local_llm_false():
    budget = TokenBudget("run-760", total_tokens=1000, per_attempt_tokens=400, total_calls=4)
    receipt = budget.receipt(spent_tokens=250, spent_calls=1, attempts=1, reason_code="initial")
    assert receipt["schema"] == TOKEN_BUDGET_RECEIPT_SCHEMA
    assert receipt["remaining"] == {"tokens": 750, "calls": 3}
    assert receipt["local_llm_started"] is False
    assert validate_receipt(receipt)["budget_key"] == receipt["budget_key"]


@pytest.mark.parametrize("kwargs", [
    {"total_tokens": 0, "per_attempt_tokens": 1},
    {"total_tokens": 10, "per_attempt_tokens": 11},
    {"total_tokens": 10, "per_attempt_tokens": 1, "exhaustion_policy": "repeat"},
])
def test_invalid_envelopes_fail_closed(kwargs):
    with pytest.raises(TokenBudgetError):
        TokenBudget("run-760", **kwargs)


def test_receipt_rejects_overspend_and_tampering():
    budget = TokenBudget("run-760", total_tokens=10, per_attempt_tokens=5)
    with pytest.raises(TokenBudgetError):
        budget.receipt(spent_tokens=11)
    receipt = budget.receipt()
    receipt["budget_key"] = "tampered"
    with pytest.raises(TokenBudgetError):
        validate_receipt(receipt)
