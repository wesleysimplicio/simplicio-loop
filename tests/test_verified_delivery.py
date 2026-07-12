import pytest

from simplicio_loop.execution_board import ExecutionBoard
from simplicio_loop.phase_events import build_phase_event
from simplicio_loop.runtime_adapter import LoopRuntimeAdapter
from simplicio_loop.verified_delivery import VerifiedAgentDelivery, VerifiedDeliveryError


class Runtime:
    def negotiate(self, request):
        return {"contract": "simplicio.runtime/v1", "contract_version": "1",
                "capabilities": ["events", "leases", "evidence", "completion"]}

    def apply(self, operation):
        return {"accepted": True, "operation_id": operation["operation_id"]}


def make_delivery(tmp_path):
    runtime = LoopRuntimeAdapter(run_id="run-168", work_item_id="wi-168", actor="agent@host",
                                 transport=Runtime(), outbox_path=tmp_path / "outbox.jsonl")
    runtime.negotiate()
    return VerifiedAgentDelivery(runtime=runtime, board=ExecutionBoard(run_id="run-168"),
                                 attempt_id="attempt-1")


def test_verified_delivery_projects_the_same_transition_to_runtime_and_board(tmp_path):
    delivery = make_delivery(tmp_path)
    for phase in ("intake", "mapping", "planning", "executing", "validating", "watching", "delivering"):
        delivery.transition(phase)
    delivery.record_evidence({"schema": "simplicio.ac-evidence/v1", "status": "PASS",
                              "ready": True, "verdict": "COMPLETE", "receipt_id": "r-168"})
    delivery.record_watcher(match=True, challenge="replay run-168")
    result = delivery.complete({"schema": "simplicio.ac-evidence/v1", "status": "PASS",
                                "ready": True, "verdict": "COMPLETE", "receipt_id": "r-168"})
    projection = delivery.board.replay()
    assert result["status"] == "VERIFIED"
    assert projection["cards"][0]["status"] == "done"
    assert projection["cards"][0]["gates"]["evidence"] is True
    assert projection["cards"][0]["gates"]["watcher"] is True


def test_completion_is_fail_closed_without_both_gates(tmp_path):
    delivery = make_delivery(tmp_path)
    for phase in ("intake", "mapping", "planning", "executing", "validating", "watching", "delivering"):
        delivery.transition(phase)
    receipt = {"schema": "simplicio.ac-evidence/v1", "status": "PASS",
               "ready": True, "verdict": "COMPLETE", "receipt_id": "r-168"}
    with pytest.raises(VerifiedDeliveryError, match="evidence and measured watcher"):
        delivery.complete(receipt)
    delivery.record_evidence(receipt)
    with pytest.raises(VerifiedDeliveryError, match="evidence and measured watcher"):
        delivery.complete(receipt)
