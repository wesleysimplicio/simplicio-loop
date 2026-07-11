import threading

import pytest

from simplicio_loop.budget import (
    BudgetExceeded,
    BudgetLedger,
    ContextPackRef,
    DELTA_SCHEMA,
    RunBudget,
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


def test_concurrent_admission_cannot_oversubscribe(tmp_path):
    budget = RunBudget("run-race", token_limit=100)
    path = tmp_path / "race.sqlite"
    results = []
    barrier = threading.Barrier(6)

    def admit(index):
        ledger = BudgetLedger(path, budget)
        barrier.wait()
        try:
            ledger.reserve("r-%d" % index, "w-%d" % index, tokens=20)
            results.append(True)
        except BudgetExceeded:
            results.append(False)

    threads = [threading.Thread(target=admit, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(results) == 5
    assert BudgetLedger(path, budget).snapshot()["reserved_tokens"] == 100


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
