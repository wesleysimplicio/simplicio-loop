"""Prove issue #288's last-named gap is closed: ``LoopRuntimeAdapter``/``VerifiedAgentDelivery``
were real, fully tested classes (``simplicio_loop/runtime_adapter.py``, ``verified_delivery.py``)
with zero references in the actual dispatch path -- completion was gated on
``execution_state == "applied"`` alone (``simplicio_loop/runner.py``), never on a genuine
evidence + watcher + delivery convergence check.

This file proves the opt-in gate (``SIMPLICIO_VERIFIED_DELIVERY_GATE=1``) actually routes a real
dispatch attempt's success/failure decision through ``VerifiedAgentDelivery.complete()`` -- not a
stub: no measured watcher pass demotes an otherwise-``applied`` attempt to ``failed``, a genuine
measured watcher pass lets it through, and with the flag off (default) behavior is unchanged.
"""
import json

from simplicio_loop import runner as runner_mod
from tests.test_dispatch_merge_wiring import (
    IDENTITY,
    _arm_fixture,
    _built_context_pack,
    _simulate_runtime_work_via_env_override,
)


def _item(repo, run_id, task_id="task-vd-1"):
    return {
        "repo": str(repo), "run_id": run_id, "task_index": 1, "worker_id": IDENTITY["agent_id"],
        "task_id": task_id,
        "agent_identity": IDENTITY,
        "context_pack": _built_context_pack(task_id, "converge PLANES ordering", ["RN01"]),
    }


def test_verified_delivery_gate_off_by_default(tmp_path, monkeypatch):
    """With the flag unset, dispatch behavior is byte-for-byte unchanged: no
    ``verified_delivery`` gating occurs even though no watcher ever ran."""
    repo, run_id = _arm_fixture(tmp_path, monkeypatch)
    _simulate_runtime_work_via_env_override(monkeypatch)
    monkeypatch.delenv("SIMPLICIO_VERIFIED_DELIVERY_GATE", raising=False)

    record = runner_mod._operator_dispatch_attempt(_item(repo, run_id))

    assert record["status"] == "succeeded"
    assert record["execution_state"] == "applied"
    assert record["verified_delivery"] is None


def test_verified_delivery_gate_demotes_success_without_a_measured_watcher(tmp_path, monkeypatch):
    """Turning the gate on with no watcher run at all must demote an otherwise-``applied``,
    ``VERIFIED``-receipt-pair attempt from ``succeeded`` to ``failed`` -- proving the gate is
    load-bearing, not a decorative pass-through."""
    repo, run_id = _arm_fixture(tmp_path, monkeypatch)
    _simulate_runtime_work_via_env_override(monkeypatch)
    monkeypatch.setenv("SIMPLICIO_VERIFIED_DELIVERY_GATE", "1")

    record = runner_mod._operator_dispatch_attempt(_item(repo, run_id))

    assert record["execution_state"] == "applied"
    assert record["receipt_status"] == "VERIFIED"
    assert record["status"] == "failed"
    gate = record["verified_delivery"]
    assert gate["verified"] is False
    assert "watcher" in gate["reason"]


def test_verified_delivery_gate_passes_with_a_real_measured_watcher(tmp_path, monkeypatch):
    """With a real, measured, matching watcher receipt on disk, the gate drives the real
    LoopRuntimeAdapter -> VerifiedAgentDelivery -> ExecutionBoard chain through every phase and
    reports the attempt VERIFIED end to end."""
    repo, run_id = _arm_fixture(tmp_path, monkeypatch)
    _simulate_runtime_work_via_env_override(monkeypatch)
    monkeypatch.setenv("SIMPLICIO_VERIFIED_DELIVERY_GATE", "1")

    status = runner_mod.read_status(str(repo), run_id)
    run_dir = status["run_dir"]
    watcher_dir = runner_mod.Path(run_dir) / "loop"
    watcher_dir.mkdir(parents=True, exist_ok=True)
    (watcher_dir / "watcher_state.json").write_text(json.dumps({
        "status": "MEASURED", "match": True, "challenge": "wch-test-vd-1",
    }), encoding="utf-8")

    record = runner_mod._operator_dispatch_attempt(_item(repo, run_id))

    assert record["status"] == "succeeded", record
    assert record["execution_state"] == "applied"
    gate = record["verified_delivery"]
    assert gate["verified"] is True
    assert gate["status"] == "VERIFIED"
    assert gate["board_status"] == "COMPLETE"
    assert gate["delivery"]["target"] == "local-fixture"
    assert gate["delivery"]["satisfied"] is True


if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_verified_delivery_dispatch_wiring")
