import pytest

from simplicio_loop.driver_contract import DriverContractError, evaluate_tick, normalize_event, reconcile_events


def test_hook_and_self_paced_normalize_to_equivalent_stream():
    base = {"event_id": "tick-1", "run_id": "r1", "iteration": 2, "phase": "validate", "decision": "continue", "reason_code": "gates_pending", "gates": {"oracle": False}}
    hook = normalize_event(base, "hook")
    paced = normalize_event(base, "self-paced")
    hook.pop("source")
    paced.pop("source")
    assert hook == paced


def test_duplicate_callback_is_idempotent_and_conflict_rejected():
    row = {"event_id": "e1", "run_id": "r", "iteration": 1, "decision": "continue"}
    assert len(reconcile_events([dict(row, source="hook"), dict(row, source="hook")])) == 1
    assert len(reconcile_events([dict(row, source="hook"), dict(row, source="self-paced")])) == 1
    with pytest.raises(DriverContractError, match="conflicting duplicate"):
        reconcile_events([dict(row, source="hook"), dict(row, source="hook", decision="stop")])


def test_missing_hook_cannot_false_complete_and_modes_share_gates():
    common = {"iteration": 1, "max_iterations": 5, "promise_exact": True, "gates": {"watcher": True, "evidence": True, "oracle": True}}
    assert evaluate_tick(dict(common, mode="hook", hook_delivered=False))["reason_code"] == "hook_missing"
    assert evaluate_tick(dict(common, mode="self-paced"))["decision"] == "stop"


def test_stop_cap_and_pending_evidence_are_shared():
    common = {"iteration": 1, "max_iterations": 2, "promise_exact": True, "gates": {"watcher": True, "evidence": False, "oracle": True}}
    for mode in ("hook", "self-paced"):
        assert evaluate_tick(dict(common, mode=mode))["reason_code"] == "gates_pending"
        assert evaluate_tick(dict(common, mode=mode, stop_requested=True))["reason_code"] == "stop_requested"
        assert evaluate_tick(dict(common, mode=mode, iteration=2))["reason_code"] == "iteration_cap"
