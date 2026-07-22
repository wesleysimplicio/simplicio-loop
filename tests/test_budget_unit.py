import sqlite3
import threading

import pytest

from simplicio_loop.budget import (
    BudgetError,
    BudgetExceeded,
    BudgetLedger,
    ContextPackRef,
    DELTA_SCHEMA,
    RunBudget,
    UnknownReservation,
    context_pack_ref,
    continuation_delta,
)


def test_atomic_shared_reservation_and_idempotent_settlement(tmp_path):
    budget = RunBudget("run-1", token_limit=100, call_limit=2, cost_limit_micros=20)
    one = BudgetLedger(tmp_path / "budget.sqlite", budget)
    two = BudgetLedger(tmp_path / "budget.sqlite", budget)
    one.reserve("r1", "w1", tokens=60, cost_micros=10)
    with pytest.raises(BudgetExceeded):
        two.reserve("r2", "w2", tokens=50, cost_micros=10)
    receipt = one.settle("r1", tokens=40, cost_micros=7)
    assert receipt["schema"] == "simplicio.usage-settlement/v1"
    assert two.settle("r1", tokens=40, cost_micros=7) == receipt
    snap = two.snapshot()
    assert snap["spent_tokens"] == 40
    assert snap["reserved_tokens"] == 0


def test_settlement_replay_is_scoped_to_its_run(tmp_path):
    path = tmp_path / "budget.sqlite"
    first = BudgetLedger(path, RunBudget("run-one", token_limit=100))
    first.reserve("shared-id", "work-one", tokens=10)
    first.settle("shared-id", tokens=10)

    second = BudgetLedger(path, RunBudget("run-two", token_limit=100))
    with pytest.raises(UnknownReservation, match="shared-id"):
        second.settle("shared-id", tokens=10)


def test_legacy_global_settlement_key_is_migrated_to_a_run_scoped_key(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(str(path)) as db:
        db.execute(
            "CREATE TABLE budget_settlements (reservation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
            "payload TEXT NOT NULL, created_at REAL NOT NULL)",
        )
        db.execute(
            "INSERT INTO budget_settlements VALUES(?,?,?,?)",
            ("old", "old-run", "{}", 0.0),
        )
    BudgetLedger(path, RunBudget("new-run", token_limit=10))
    with sqlite3.connect(str(path)) as db:
        key_columns = [row[1] for row in sorted(db.execute("PRAGMA table_info(budget_settlements)"), key=lambda row: row[5]) if row[5]]
        assert key_columns == ["run_id", "reservation_id"]
        assert db.execute("SELECT reservation_id,run_id FROM budget_settlements").fetchone() == ("old", "old-run")


def test_concurrent_admission_cannot_oversubscribe(tmp_path):
    budget = RunBudget("run-race", token_limit=100)
    path = tmp_path / "race.sqlite"
    results = []
    errors = []
    barrier = threading.Barrier(6)

    def admit(index):
        try:
            barrier.wait(timeout=5)
            ledger = BudgetLedger(path, budget)
            ledger.reserve("r-%d" % index, "w-%d" % index, tokens=20)
            results.append(True)
        except BudgetExceeded:
            results.append(False)
        except Exception as exc:  # failure evidence; never strand peers at an unbounded barrier
            errors.append((type(exc).__name__, str(exc)))

    threads = [
        threading.Thread(target=admit, args=(i,), name="budget-init-%d" % i, daemon=True)
        for i in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert sum(results) == 5
    assert BudgetLedger(path, budget).snapshot()["reserved_tokens"] == 100


def test_concurrent_constructors_migrate_legacy_settlements_once(tmp_path):
    path = tmp_path / "legacy-race.sqlite"
    receipt = '{"reservation_id":"old","run_id":"old-run"}'
    with sqlite3.connect(str(path)) as db:
        db.execute(
            "CREATE TABLE budget_settlements (reservation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
            "payload TEXT NOT NULL, created_at REAL NOT NULL)",
        )
        db.execute("INSERT INTO budget_settlements VALUES(?,?,?,?)", ("old", "old-run", receipt, 0.0))

    barrier = threading.Barrier(8)
    errors = []

    def construct(index):
        try:
            barrier.wait(timeout=5)
            BudgetLedger(path, RunBudget("run-%d" % index, token_limit=10))
        except Exception as exc:
            errors.append((type(exc).__name__, str(exc)))

    threads = [threading.Thread(target=construct, args=(index,), daemon=True) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    with sqlite3.connect(str(path)) as db:
        key_columns = [
            row[1]
            for row in sorted(db.execute("PRAGMA table_info(budget_settlements)"), key=lambda row: row[5])
            if row[5]
        ]
        assert key_columns == ["run_id", "reservation_id"]
        assert db.execute(
            "SELECT payload FROM budget_settlements WHERE run_id=? AND reservation_id=?",
            ("old-run", "old"),
        ).fetchone() == (receipt,)


def test_context_pack_hash_is_reused_only_for_same_inputs():
    first = context_pack_ref(goal="ship", policy={"mode": "safe"}, acceptance=["green"], relevant_fingerprint="tree-a")
    same = context_pack_ref(goal="ship", policy={"mode": "safe"}, acceptance=["green"], relevant_fingerprint="tree-a")
    changed = context_pack_ref(goal="ship", policy={"mode": "safe"}, acceptance=["green"], relevant_fingerprint="tree-b")
    assert first.pack_hash == same.pack_hash
    assert first.relevant_fingerprint == same.relevant_fingerprint
    assert first.pack_hash == changed.pack_hash
    assert first.relevant_fingerprint != changed.relevant_fingerprint


def test_continuation_sends_delta_after_ack_and_detects_full_history():
    pack = ContextPackRef("pack", "goal", "tree")
    result = continuation_delta([{"seq": 1, "kind": "start"}, {"seq": 2, "kind": "done"}], 1, pack=pack)
    assert result["schema"] == DELTA_SCHEMA
    assert result["full_history"] is False
    assert [event["seq"] for event in result["events"]] == [2]
    forced = continuation_delta(result["events"], 1, pack=pack, force_full=True)
    assert forced["full_history"] is True
    assert forced["events"] == [{"seq": 2, "kind": "done"}]


# -- RunBudget validation ------------------------------------------------

def test_run_budget_rejects_blank_run_id():
    with pytest.raises(ValueError, match="run_id"):
        RunBudget("  ", token_limit=10)


def test_run_budget_rejects_negative_limits():
    with pytest.raises(ValueError, match="token_limit"):
        RunBudget("run-1", token_limit=-1)


def test_run_budget_rejects_unsupported_exhaustion_policy():
    with pytest.raises(ValueError, match="exhaustion policy"):
        RunBudget("run-1", token_limit=10, exhaustion_policy="explode")


# -- envelope immutability -------------------------------------------------

def test_run_budget_envelope_is_immutable_after_freeze(tmp_path):
    path = tmp_path / "budget.sqlite"
    BudgetLedger(path, RunBudget("run-1", token_limit=100))
    with pytest.raises(BudgetError, match="immutable"):
        BudgetLedger(path, RunBudget("run-1", token_limit=200))


def test_reopening_ledger_with_identical_envelope_is_fine(tmp_path):
    path = tmp_path / "budget.sqlite"
    budget = RunBudget("run-1", token_limit=100)
    BudgetLedger(path, budget)
    reopened = BudgetLedger(path, budget)
    assert reopened.snapshot()["limits"] == budget.as_dict()


# -- reservation idempotency and conflicts --------------------------------

def test_reservation_id_replayed_with_same_estimate_is_idempotent(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", RunBudget("run-1", token_limit=100))
    first = ledger.reserve("r1", "w1", tokens=10)
    again = ledger.reserve("r1", "w1", tokens=10)
    assert again["reservation_id"] == first["reservation_id"]
    assert again["work_item_id"] == first["work_item_id"]
    assert again["estimate_tokens"] == first["tokens"] == 10
    assert ledger.snapshot()["reserved_tokens"] == 10


def test_reservation_id_reused_with_different_estimate_raises(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", RunBudget("run-1", token_limit=100))
    ledger.reserve("r1", "w1", tokens=10)
    with pytest.raises(BudgetError, match="reused with different estimate"):
        ledger.reserve("r1", "w1", tokens=20)


def test_reserve_respects_call_and_latency_limits(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", RunBudget("run-1", token_limit=1000, call_limit=1))
    ledger.reserve("r1", "w1", tokens=1, calls=1)
    with pytest.raises(BudgetExceeded):
        ledger.reserve("r2", "w2", tokens=1, calls=1)
    latency_ledger = BudgetLedger(tmp_path / "latency.sqlite", RunBudget("run-2", token_limit=1000, latency_limit_ms=100))
    latency_ledger.reserve("l1", "w1", tokens=1, latency_ms=100)
    with pytest.raises(BudgetExceeded):
        latency_ledger.reserve("l2", "w2", tokens=1, latency_ms=1)


# -- settlement failure paths ----------------------------------------------

def test_settle_unknown_reservation_raises(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", RunBudget("run-1", token_limit=100))
    with pytest.raises(UnknownReservation):
        ledger.settle("missing", tokens=1)


def test_settle_twice_is_idempotent_but_double_settle_state_is_rejected(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", RunBudget("run-1", token_limit=100))
    ledger.reserve("r1", "w1", tokens=10)
    receipt = ledger.settle("r1", tokens=10)
    assert ledger.settle("r1", tokens=10) == receipt


def test_settle_cancelled_reservation_is_not_settleable(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", RunBudget("run-1", token_limit=100))
    ledger.reserve("r1", "w1", tokens=10)
    assert ledger.cancel("r1") is True
    with pytest.raises(BudgetError, match="not settleable"):
        ledger.settle("r1", tokens=10)


def test_settle_rejects_late_usage_that_would_overspend(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", RunBudget("run-1", token_limit=10))
    ledger.reserve("r1", "w1", tokens=5)
    with pytest.raises(BudgetExceeded, match="overspend"):
        ledger.settle("r1", tokens=50)


def test_settle_keeps_other_active_reservations_inside_shared_limit(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", RunBudget("run-1", token_limit=100))
    ledger.reserve("r1", "w1", tokens=50)
    ledger.reserve("r2", "w2", tokens=50)

    with pytest.raises(BudgetExceeded, match="overspend"):
        ledger.settle("r1", tokens=100)

    snapshot = ledger.snapshot()
    assert snapshot["spent_tokens"] == 0
    assert snapshot["reserved_tokens"] == 100
    receipt = ledger.settle("r1", tokens=50)
    assert receipt["tokens"] == 50
    assert ledger.snapshot()["reserved_tokens"] == 50


@pytest.mark.parametrize(
    ("budget_kwargs", "reserve_kwargs", "settle_kwargs"),
    [
        ({"call_limit": 2}, {"calls": 1}, {"calls": 2}),
        ({"cost_limit_micros": 20}, {"cost_micros": 10}, {"cost_micros": 20}),
        ({"latency_limit_ms": 100}, {"latency_ms": 50}, {"latency_ms": 100}),
    ],
)
def test_settle_accounts_for_other_reservations_in_auxiliary_limits(
    tmp_path, budget_kwargs, reserve_kwargs, settle_kwargs,
):
    budget = RunBudget("run-aux", token_limit=1000, **budget_kwargs)
    ledger = BudgetLedger(tmp_path / "budget.sqlite", budget)
    ledger.reserve("r1", "w1", tokens=1, **reserve_kwargs)
    ledger.reserve("r2", "w2", tokens=1, **reserve_kwargs)

    usage = {"tokens": 1, "calls": 1, "cost_micros": 0, "latency_ms": 0}
    usage.update(settle_kwargs)
    with pytest.raises(BudgetExceeded, match="overspend"):
        ledger.settle("r1", **usage)


# -- cancellation ------------------------------------------------------------

def test_cancel_unknown_reservation_raises(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", RunBudget("run-1", token_limit=100))
    with pytest.raises(UnknownReservation):
        ledger.cancel("missing")


def test_cancel_already_settled_reservation_returns_false(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", RunBudget("run-1", token_limit=100))
    ledger.reserve("r1", "w1", tokens=10)
    ledger.settle("r1", tokens=10)
    assert ledger.cancel("r1") is False


def test_cancel_frees_reserved_capacity_for_new_reservations(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", RunBudget("run-1", token_limit=10))
    ledger.reserve("r1", "w1", tokens=10)
    with pytest.raises(BudgetExceeded):
        ledger.reserve("r2", "w2", tokens=1)
    ledger.cancel("r1")
    ledger.reserve("r3", "w3", tokens=10)
    assert ledger.snapshot()["reserved_tokens"] == 10


# -- continuation_delta validation ------------------------------------------

def test_continuation_delta_rejects_negative_cursor():
    pack = ContextPackRef("pack", "goal", "tree")
    with pytest.raises(ValueError, match="acknowledged_cursor"):
        continuation_delta([], -1, pack=pack)


def test_continuation_delta_rejects_non_positive_seq():
    pack = ContextPackRef("pack", "goal", "tree")
    with pytest.raises(BudgetError, match="positive integer seq"):
        continuation_delta([{"seq": 0, "kind": "start"}], 0, pack=pack)
