import pytest

from simplicio_loop.execution_board import ExecutionBoard
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
    delivery.record_delivery({"target": "local-fixture", "satisfied": True})
    result = delivery.complete({"schema": "simplicio.ac-evidence/v1", "status": "PASS",
                                "ready": True, "verdict": "COMPLETE", "receipt_id": "r-168"})
    projection = delivery.board.replay()
    assert result["status"] == "VERIFIED"
    assert result["delivery"]["target"] == "local-fixture"
    assert projection["cards"][0]["status"] == "done"
    assert projection["cards"][0]["gates"]["evidence"] is True
    assert projection["cards"][0]["gates"]["watcher"] is True
    assert projection["cards"][0]["delivery"]["convergence"] == "local-fixture"


def test_completion_is_fail_closed_without_both_gates(tmp_path):
    delivery = make_delivery(tmp_path)
    for phase in ("intake", "mapping", "planning", "executing", "validating", "watching", "delivering"):
        delivery.transition(phase)
    receipt = {"schema": "simplicio.ac-evidence/v1", "status": "PASS",
               "ready": True, "verdict": "COMPLETE", "receipt_id": "r-168"}
    with pytest.raises(VerifiedDeliveryError, match="evidence and measured watcher"):
        delivery.complete(receipt)
    delivery.record_evidence(receipt)
    delivery.record_delivery({"target": "local-fixture", "satisfied": True})
    with pytest.raises(VerifiedDeliveryError, match="evidence and measured watcher"):
        delivery.complete(receipt)


def test_external_delivery_requires_merge_queue_acceptance_evidence(tmp_path):
    delivery = make_delivery(tmp_path)
    for phase in ("intake", "mapping", "planning", "executing", "validating", "watching", "delivering"):
        delivery.transition(phase)
    receipt = {"schema": "simplicio.ac-evidence/v1", "status": "PASS",
               "ready": True, "verdict": "COMPLETE", "receipt_id": "r-168"}
    delivery.record_evidence(receipt)
    delivery.record_watcher(match=True, challenge="replay run-168")
    delivery.record_delivery({"target": "merge-queue", "satisfied": True})
    with pytest.raises(VerifiedDeliveryError, match="merge-queue acceptance evidence"):
        delivery.complete(receipt)
    delivery.record_delivery({"target": "merge-queue", "satisfied": True,
                              "merge_queue_receipt_sha": "abc123", "merge_queue_status": "accepted"})
    with pytest.raises(VerifiedDeliveryError, match="merge-queue worktree/branch evidence"):
        delivery.complete(receipt)
    delivery.record_delivery({"target": "merge-queue", "satisfied": True,
                              "merge_queue": {"receipt_sha": "abc123", "status": "accepted",
                                              "branch": "simplicio/run-168/wi-168",
                                              "worktree_path": "/tmp/run-168/wi-168"}})
    result = delivery.complete(receipt)
    assert result["delivery"]["merge_queue_status"] == "accepted"
    assert result["delivery"]["merge_queue"]["branch"] == "simplicio/run-168/wi-168"
