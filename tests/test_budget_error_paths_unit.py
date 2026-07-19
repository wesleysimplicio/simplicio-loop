"""Unit coverage for BudgetLedger's fail-closed error branches (reservation reuse mismatch,
exhaustion on reserve/settle, unknown reservation, non-settleable state, cancel-of-settled).
These are pure sqlite-on-tmp-path paths — no subprocess, no network — filling gaps left by the
existing happy-path tests in test_budget.py.
"""
import pytest

from simplicio_loop.budget import (
    BudgetExceeded,
    BudgetError,
    BudgetLedger,
    RunBudget,
    UnknownReservation,
)


def _ledger(tmp_path, **limits):
    budget = RunBudget(run_id="run-1", token_limit=limits.get("token_limit", 1000),
                       call_limit=limits.get("call_limit", 0),
                       cost_limit_micros=limits.get("cost_limit_micros", 0),
                       latency_limit_ms=limits.get("latency_limit_ms", 0))
    return BudgetLedger(tmp_path / "budget.db", budget)


def test_reserve_same_id_same_estimate_is_idempotent(tmp_path):
    ledger = _ledger(tmp_path)
    first = ledger.reserve("r1", "wi-1", tokens=10)
    second = ledger.reserve("r1", "wi-1", tokens=10)
    assert first["reservation_id"] == second["reservation_id"] == "r1"
    assert first["work_item_id"] == second["work_item_id"] == "wi-1"


def test_reserve_same_id_different_estimate_raises(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.reserve("r1", "wi-1", tokens=10)
    with pytest.raises(BudgetError):
        ledger.reserve("r1", "wi-1", tokens=999)


def test_reserve_over_token_limit_raises_exceeded(tmp_path):
    ledger = _ledger(tmp_path, token_limit=5)
    with pytest.raises(BudgetExceeded):
        ledger.reserve("r1", "wi-1", tokens=10)


def test_reserve_over_call_limit_raises_exceeded(tmp_path):
    ledger = _ledger(tmp_path, call_limit=1)
    ledger.reserve("r1", "wi-1", tokens=1, calls=1)
    with pytest.raises(BudgetExceeded):
        ledger.reserve("r2", "wi-1", tokens=1, calls=1)


def test_reserve_rejects_invalid_ids_and_negative_estimates(tmp_path):
    ledger = _ledger(tmp_path)
    with pytest.raises(ValueError):
        ledger.reserve("", "wi-1", tokens=1)
    with pytest.raises(ValueError):
        ledger.reserve("r1", "wi-1", tokens=-1)


def test_settle_unknown_reservation_raises(tmp_path):
    ledger = _ledger(tmp_path)
    with pytest.raises(UnknownReservation):
        ledger.settle("ghost", tokens=1)


def test_settle_rejects_negative_usage(tmp_path):
    ledger = _ledger(tmp_path)
    with pytest.raises(ValueError):
        ledger.settle("r1", tokens=-5)


def test_settle_is_idempotent_on_replay(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.reserve("r1", "wi-1", tokens=10)
    first = ledger.settle("r1", tokens=10)
    second = ledger.settle("r1", tokens=10)
    assert first == second


def test_settle_of_a_cancelled_reservation_is_not_settleable(tmp_path):
    # Settling twice is idempotent (covered above), not an error. The "not settleable" branch
    # requires a reservation whose state has moved off "reserved" without a settlement payload
    # already on file — a cancelled reservation is exactly that.
    ledger = _ledger(tmp_path)
    ledger.reserve("r2", "wi-1", tokens=5)
    ledger.cancel("r2")
    with pytest.raises(BudgetError):
        ledger.settle("r2", tokens=5)


def test_settle_over_token_limit_raises_exceeded(tmp_path):
    ledger = _ledger(tmp_path, token_limit=10)
    ledger.reserve("r1", "wi-1", tokens=5)
    with pytest.raises(BudgetExceeded):
        ledger.settle("r1", tokens=20)


def test_cancel_unknown_reservation_raises(tmp_path):
    ledger = _ledger(tmp_path)
    with pytest.raises(UnknownReservation):
        ledger.cancel("ghost")


def test_cancel_reserved_returns_true_then_false_on_replay(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.reserve("r1", "wi-1", tokens=5)
    assert ledger.cancel("r1") is True
    assert ledger.cancel("r1") is False


def test_cancel_settled_reservation_returns_false(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.reserve("r1", "wi-1", tokens=5)
    ledger.settle("r1", tokens=5)
    assert ledger.cancel("r1") is False
