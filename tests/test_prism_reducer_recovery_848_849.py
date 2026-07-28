from __future__ import annotations

import itertools

import pytest

import simplicio_loop.prism_recovery as recovery_module
from simplicio_loop.hbp_ledger import canonical_sha256
from simplicio_loop.prism_contracts import encode_hbp_frame
from simplicio_loop.prism_recovery import (
    PrismJournal,
    PrismRecoveryError,
    RecoveryEvent,
    assert_current_fence,
    reconcile_orphan_intents,
    recover_state,
)
from simplicio_loop.prism_reducer import (
    ExpectedTask,
    PrismReducer,
    PrismReducerError,
    TaskResult,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def expected(
    task_id, *, slot="slot", owner=None, fence=1, generation="gen", depends=()
):
    return ExpectedTask(
        task_id,
        slot,
        owner or f"agent-{task_id}",
        fence,
        generation,
        depends,
    )


def result(
    task_id,
    *,
    slot="slot",
    owner=None,
    fence=1,
    generation="gen",
    verdict="accepted",
    writes=(),
    test_hash=SHA_B,
):
    return TaskResult(
        task_id,
        slot,
        owner or f"agent-{task_id}",
        1,
        fence,
        generation,
        verdict,
        SHA_A,
        (SHA_C,),
        writes,
        test_hash,
    )


def test_reducer_is_permutation_invariant_and_never_promotes_completion():
    expected_tasks = [
        expected("a"),
        expected("b"),
        expected("c", depends=("a",)),
    ]
    digest = None
    for order in itertools.permutations(("a", "b", "c")):
        reducer = PrismReducer(expected_tasks)
        for task_id in order:
            assert reducer.submit(result(task_id)) == "ACCEPTED_FOR_REDUCTION"
        slot_receipt = reducer.reduce_slot("slot")
        assert slot_receipt["verdict"] == "accepted"
        assert slot_receipt["zero_loss"] is True
        assert slot_receipt["completion_promoted"] is False
        digest = digest or slot_receipt["receipt_hash"]
        assert slot_receipt["receipt_hash"] == digest
        prism_receipt = reducer.reduce_prism(("slot",))
        assert prism_receipt["completion_oracle_input_ready"] is True
        assert prism_receipt["completion_promoted"] is False


def test_missing_tamper_cross_slot_stale_and_duplicate_are_bounded():
    reducer = PrismReducer([expected("a"), expected("b")])
    reducer.submit(result("a"))
    partial = reducer.reduce_slot("slot")
    assert partial["verdict"] == "partial"
    assert partial["missing_task_ids"] == ["b"]
    assert reducer.submit(result("a")) == "IDEMPOTENT_REPLAY"
    assert reducer.reduce_prism(("slot",))["replay_count"] == 1
    with pytest.raises(PrismReducerError, match="changed"):
        reducer.submit(result("a", verdict="failed"))
    for bad, message in (
        (result("missing"), "unknown"),
        (result("b", slot="other"), "crosses"),
        (result("b", owner="other"), "owner"),
        (result("b", fence=2), "fence"),
        (result("b", generation="other"), "generation"),
    ):
        with pytest.raises(PrismReducerError, match=message):
            reducer.submit(bad)


def test_conflicts_dependencies_child_failure_and_missing_tests_fail_closed():
    conflict = PrismReducer([expected("a"), expected("b")])
    conflict.submit(result("a", writes=("x.py:symbol",)))
    conflict.submit(result("b", writes=("x.py:symbol",)))
    receipt = conflict.reduce_slot("slot")
    assert receipt["verdict"] == "blocked"
    assert receipt["reason_code"] == "COMPOSITION_CONFLICT"

    dependency = PrismReducer([expected("a"), expected("b", depends=("a",))])
    dependency.submit(result("a", verdict="failed"))
    dependency.submit(result("b"))
    assert dependency.reduce_slot("slot")["reason_code"] == "DEPENDENCY_NOT_ACCEPTED"

    child = PrismReducer([expected("a")])
    child.submit(result("a", verdict="cancelled"))
    assert child.reduce_slot("slot")["reason_code"] == "CHILD_NOT_ACCEPTED"

    tests = PrismReducer([expected("a")])
    tests.submit(result("a", test_hash=None))
    assert tests.reduce_slot("slot")["reason_code"] == "IMPACT_TEST_RECEIPT_MISSING"


def test_reducer_contract_validation_boundaries():
    with pytest.raises(PrismReducerError):
        PrismReducer([])
    with pytest.raises(PrismReducerError, match="duplicate"):
        PrismReducer([expected("a"), expected("a")])
    with pytest.raises(PrismReducerError, match="unknown"):
        PrismReducer([expected("a", depends=("missing",))])
    with pytest.raises(PrismReducerError, match="cycle"):
        PrismReducer([expected("a", depends=("b",)), expected("b", depends=("a",))])
    with pytest.raises(PrismReducerError, match="empty slot"):
        PrismReducer([expected("a")]).reduce_slot("missing")
    with pytest.raises(PrismReducerError, match="one slot"):
        PrismReducer([expected("a")]).reduce_prism(())
    with pytest.raises(PrismReducerError):
        expected("a", fence=0)
    with pytest.raises(PrismReducerError):
        ExpectedTask("", "s", "a", 1, "g")
    with pytest.raises(PrismReducerError):
        TaskResult("a", "s", "a", 1, 1, "g", "invented", SHA_A)
    with pytest.raises(PrismReducerError):
        TaskResult("a", "s", "a", 1, 1, "g", "accepted", "bad")


def event(event_type, *, task_id="a", fence=1, payload=None):
    return RecoveryEvent(
        aggregate_id=task_id,
        event_type=event_type,
        prism_id="prism",
        slot_id="slot",
        task_id=task_id,
        attempt=1,
        fence=fence,
        payload=payload or {},
    )


def test_journal_replay_checkpoint_and_state_digest_are_deterministic(tmp_path):
    clock = [1]
    journal = PrismJournal(tmp_path / "journal.hbp", clock_ns=lambda: clock[0])
    journal.append(event("task_queued"))
    clock[0] += 1
    journal.append(event("task_started"))
    clock[0] += 1
    journal.append(event("lease_takeover", fence=2))
    clock[0] += 1
    journal.append(
        event(
            "task_terminal",
            fence=2,
            payload={"state": "accepted", "receipt_hash": SHA_A},
        )
    )
    rows = journal.replay()
    state = recover_state(rows)
    assert state["tasks"] == {"a": "accepted"}
    assert state["fences"] == {"a": 2}
    assert state["active_children"] == []
    assert_current_fence(state, "a", 2)
    with pytest.raises(PrismRecoveryError, match="STALE"):
        assert_current_fence(state, "a", 1)
    checkpoint = journal.checkpoint(state, prism_id="prism")
    assert checkpoint["payload"]["covered_sequence"] == 4
    assert journal.doctor()["status"] == "VERIFIED"


def test_orphan_effect_is_reconciled_without_reexecution(tmp_path):
    journal = PrismJournal(tmp_path / "journal.hbp")
    journal.effect_intent(
        prism_id="prism",
        slot_id="slot",
        task_id="a",
        attempt=1,
        fence=1,
        effect_id="effect-a",
        effect_hash=SHA_A,
    )
    required = reconcile_orphan_intents(journal, lambda _effect: None)
    assert required["status"] == "RECOVERY_REQUIRED"
    assert required["effects_reexecuted"] == 0
    reconciled = reconcile_orphan_intents(journal, lambda _effect: SHA_B)
    assert reconciled["status"] == "RECONCILED"
    assert reconciled["reconciled_effects"] == ["effect-a"]
    state = recover_state(journal.replay())
    assert state["orphan_intents"] == []
    assert state["effect_receipts"] == ["effect-a"]


@pytest.mark.parametrize("mutation", ["tail", "payload", "magic"])
def test_corrupt_or_truncated_journal_fails_closed(tmp_path, mutation):
    path = tmp_path / "journal.hbp"
    journal = PrismJournal(path)
    journal.append(event("task_queued"))
    raw = bytearray(path.read_bytes())
    if mutation == "tail":
        raw = raw[:-1]
    elif mutation == "payload":
        raw[20] ^= 1
    else:
        raw[0:4] = b"NOPE"
    path.write_bytes(raw)
    with pytest.raises(PrismRecoveryError):
        journal.replay()
    doctor = journal.doctor()
    assert doctor["status"] == "CORRUPT"
    assert doctor["event_count"] is None


def test_recovery_never_infers_terminal_or_accepts_bad_effect_order(tmp_path):
    with pytest.raises(PrismRecoveryError, match="receipt"):
        recover_state(
            [
                event("task_terminal", payload={"state": "accepted"}).to_dict()
                | {"event_type": "task_terminal"}
            ]
        )
    with pytest.raises(PrismRecoveryError, match="prior intent"):
        recover_state(
            [
                event("effect_receipt", payload={"effect_id": "x"}).to_dict()
                | {"event_type": "effect_receipt"}
            ]
        )
    with pytest.raises(PrismRecoveryError, match="missing id"):
        recover_state(
            [event("effect_intent").to_dict() | {"event_type": "effect_intent"}]
        )
    with pytest.raises(PrismRecoveryError, match="regressed"):
        recover_state(
            [
                event("lease_acquired", fence=2).to_dict()
                | {"event_type": "lease_acquired"},
                event("lease_heartbeat", fence=1).to_dict()
                | {"event_type": "lease_heartbeat"},
            ]
        )


def test_recovery_event_and_journal_limits_validate(tmp_path):
    with pytest.raises(PrismRecoveryError):
        RecoveryEvent("", "task_queued", {}, "prism")
    with pytest.raises(PrismRecoveryError):
        RecoveryEvent("a", "invented", {}, "prism")
    with pytest.raises(PrismRecoveryError):
        RecoveryEvent("a", "task_queued", {}, "prism", attempt=0)
    with pytest.raises(PrismRecoveryError):
        RecoveryEvent("a", "task_queued", {}, "prism", fence=0)
    with pytest.raises(PrismRecoveryError):
        PrismJournal(tmp_path / "x", max_frame_bytes=0)


def test_journal_io_and_frame_boundaries_fail_closed(tmp_path, monkeypatch):
    unreadable = tmp_path / "unreadable.hbp"
    unreadable.write_bytes(b"x")

    def fail_read(_path):
        raise OSError("denied")

    monkeypatch.setattr(type(unreadable), "read_bytes", fail_read)
    with pytest.raises(PrismRecoveryError, match="unreadable"):
        PrismJournal(unreadable).replay()
    monkeypatch.undo()

    truncated = tmp_path / "header.hbp"
    truncated.write_bytes(b"SPH1")
    with pytest.raises(PrismRecoveryError, match="truncated header"):
        PrismJournal(truncated).replay()

    oversized = tmp_path / "oversized.hbp"
    PrismJournal(oversized).append(event("task_queued"))
    with pytest.raises(PrismRecoveryError, match="exceeds limit"):
        PrismJournal(oversized, max_frame_bytes=1).replay()

    bounded = PrismJournal(tmp_path / "bounded.hbp", max_frame_bytes=64)
    with pytest.raises(PrismRecoveryError, match="encoded journal frame"):
        bounded.append(event("task_queued", payload={"large": "x" * 128}))


def test_hash_chain_and_partial_write_fail_closed(tmp_path, monkeypatch):
    tampered = tmp_path / "tampered.hbp"
    journal = PrismJournal(tampered)
    journal.append(event("task_queued"))
    row = journal.replay()[0]
    row["sequence"] = 2
    row["event_hash"] = canonical_sha256(
        {key: value for key, value in row.items() if key != "event_hash"}
    )
    tampered.write_bytes(encode_hbp_frame(row))
    with pytest.raises(PrismRecoveryError, match="hash-chain"):
        journal.replay()

    partial = PrismJournal(tmp_path / "partial.hbp")
    monkeypatch.setattr(
        recovery_module.os,
        "write",
        lambda _descriptor, frame: len(frame) - 1,
    )
    with pytest.raises(PrismRecoveryError, match="partial journal write"):
        partial.append(event("task_queued"))


def test_recover_state_ignores_non_task_checkpoint():
    checkpoint = RecoveryEvent(
        aggregate_id="prism",
        event_type="checkpoint",
        prism_id="prism",
        payload={"covered_sequence": 0},
    )
    state = recover_state([checkpoint.to_dict()])
    assert state["tasks"] == {}
    assert state["fences"] == {}
