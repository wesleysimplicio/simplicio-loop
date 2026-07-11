from __future__ import annotations

import json

import pytest

from simplicio_loop.phase_events import build_phase_event
from simplicio_loop.runtime_adapter import (
    LoopRuntimeAdapter,
    RuntimeAdapterError,
    RuntimeCompatibilityError,
)


class FakeRuntime:
    def __init__(self, version="1", fail=False):
        self.version = version
        self.fail = fail
        self.operations = []

    def negotiate(self, request):
        return {"contract": "simplicio.runtime/v1", "contract_version": self.version,
                "capabilities": ["events", "leases", "evidence", "completion"]}

    def apply(self, operation):
        if self.fail:
            raise OSError("runtime offline")
        self.operations.append(operation)
        return {"accepted": True, "operation_id": operation["operation_id"]}


def adapter(tmp_path, runtime):
    return LoopRuntimeAdapter(run_id="run-1", work_item_id="wi-1", actor="codex@host-a",
                              transport=runtime, outbox_path=tmp_path / "outbox.jsonl")


@pytest.mark.parametrize("version", ["0", "2"])
def test_negotiation_rejects_n_minus_1_and_n_plus_1_before_mutation(tmp_path, version):
    runtime = FakeRuntime(version=version)
    bridge = adapter(tmp_path, runtime)
    with pytest.raises(RuntimeCompatibilityError, match="incompatible runtime contract"):
        bridge.negotiate()
    assert runtime.operations == []


def test_negotiation_rejects_missing_capability(tmp_path):
    runtime = FakeRuntime()
    runtime.negotiate = lambda request: {"contract": "simplicio.runtime/v1", "contract_version": "1", "capabilities": ["events"]}
    with pytest.raises(RuntimeCompatibilityError, match="missing required capabilities"):
        adapter(tmp_path, runtime).negotiate()


def test_round_trip_preserves_identity_and_event_fields(tmp_path):
    runtime = FakeRuntime()
    bridge = adapter(tmp_path, runtime)
    bridge.negotiate()
    event = build_phase_event(run_id="run-1", work_item_id="wi-1", actor="codex@host-a",
                              cause="operator", sequence=1, event_id="e-1", from_phase=None,
                              to_phase="intake")
    result = bridge.emit_event(event)
    assert result["status"] == "DELIVERED"
    operation = runtime.operations[0]
    assert operation["run_id"] == operation["payload"]["run_id"] == "run-1"
    assert operation["work_item_id"] == operation["payload"]["work_item_id"] == "wi-1"
    assert operation["payload"]["event_id"] == "e-1"


def test_outage_buffers_and_reconcile_is_idempotent(tmp_path):
    runtime = FakeRuntime(fail=True)
    bridge = adapter(tmp_path, runtime)
    bridge.negotiate()
    result = bridge.register_run({"source": "issue-177"})
    assert result["status"] == "BUFFERED" and bridge.mode == "degraded"
    assert len((tmp_path / "outbox.jsonl").read_text().splitlines()) == 1
    runtime.fail = False
    replay = bridge.reconcile()
    assert replay["status"] == "MEASURED" and replay["replayed"] == 1
    assert bridge.reconcile()["replayed"] == 0


def test_standalone_is_explicit_and_does_not_claim_runtime_delivery(tmp_path):
    bridge = LoopRuntimeAdapter(run_id="run-1", work_item_id="wi-1", actor="claude@host-b",
                                standalone=True, outbox_path=tmp_path / "standalone.jsonl")
    result = bridge.register_run({"source": "local"})
    assert result["status"] == "STANDALONE" and bridge.mode == "standalone"
    assert json.loads((tmp_path / "standalone.jsonl").read_text())["kind"] == "register_run"
    with pytest.raises(RuntimeAdapterError, match="COMPLETE receipt"):
        bridge.complete({"ready": False, "verdict": "DELIVERY_PENDING"})


def test_event_identity_mismatch_is_rejected(tmp_path):
    bridge = adapter(tmp_path, FakeRuntime())
    event = build_phase_event(run_id="other", work_item_id="wi-1", actor="codex@host-a",
                              cause="operator", sequence=1, event_id="e-1", from_phase=None,
                              to_phase="intake")
    with pytest.raises(RuntimeAdapterError, match="event identity"):
        bridge.emit_event(event)


def test_runtime_mutation_requires_explicit_negotiation(tmp_path):
    bridge = adapter(tmp_path, FakeRuntime())
    with pytest.raises(RuntimeCompatibilityError, match="negotiate before"):
        bridge.register_run({"source": "issue-177"})
