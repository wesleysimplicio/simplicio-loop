"""Issue #287 (final slice): prove ``model_router.route()``'s selection is actually
threaded through the real dispatch path in ``simplicio_loop/runner.py`` -- not merely
available as a standalone primitive (``model_router.py``/``model_registry.py``) that
nothing ever calls.

Mirrors the opt-in-gate pattern already proven for #288 in
``tests/test_dispatch_merge_wiring.py``: off by default (byte-identical to before),
and when ``SIMPLICIO_MODEL_ROUTED_DISPATCH=1`` a real routing-decision-receipt (and,
when a real driver is wired for the selection, a real runtime-execution-receipt) is
computed and persisted for every dispatch attempt -- without ever blocking the
underlying dev-cli operator mutation this repo's actual apply/verify contract still
performs.
"""
import json

import pytest

from simplicio_loop import runner as runner_mod
from simplicio_loop.model_registry import ModelCapabilityRegistry
from simplicio_loop.receipt_verifier import ReceiptStatus, verify_receipt
from simplicio_loop.runtime_execution_receipt import RUNTIME_EXECUTION_RECEIPT_SCHEMA


class _FakeResult:
    def __init__(self, ok):
        self.ok = ok
        self.exit_status = 0 if ok else 1
        self.stop_reason = "completed" if ok else "error"
        self.duration_seconds = 1.5
        self.stdout = "PING_OK" if ok else ""
        self.stderr = ""
        self.error = "" if ok else "boom"
        self.resolved_model = {"runtime": "codex", "provider": "openai", "model_id": "UNAVAILABLE", "verified": False}
        self.usage = {"tokens": 42, "cost_usd": "UNAVAILABLE", "latency_seconds": 1.5}
        self.argv = ["codex", "exec", "--json", "<prompt>"]


class _FakeDriver:
    name = "codex"
    binary = "codex"

    def __init__(self, ok=True):
        self._ok = ok

    def is_installed(self):
        return True

    def version(self):
        return "codex-cli 0.0.0-test"

    def execute(self, prompt, cwd=None, timeout=180):
        return _FakeResult(self._ok)

    def build_receipt(self, **kwargs):
        from simplicio_loop.runtime_execution_receipt import build_runtime_execution_receipt
        result = kwargs.pop("result")
        return build_runtime_execution_receipt(
            route_id=kwargs["route_id"], requested=kwargs["requested"], resolved=result.resolved_model,
            driver={"name": self.name, "binary": self.binary, "version": self.version(), "identity_verified": True},
            session=kwargs["session"], argv_redacted=result.argv, env_allowlist=[], tree=kwargs["tree"],
            exit_status=result.exit_status, duration_seconds=result.duration_seconds,
            stop_reason=result.stop_reason, usage=result.usage,
        )


def test_execute_routed_runtime_writes_real_receipts_when_driver_available(tmp_path, monkeypatch):
    registry = ModelCapabilityRegistry([
        {"runtime": "codex", "provider": "openai", "model_id": "gpt-5.6",
         "capabilities": ["execute"], "probe": {"kind": "binary_on_path", "target": "true"}},
    ])
    monkeypatch.setattr(runner_mod, "driver_for_runtime", lambda runtime: _FakeDriver(ok=True))
    item = {
        "repo": str(tmp_path), "task_id": "task-1", "task_index": 1, "worker_id": "w1",
        "context_pack": {"goal": "reply with PING_OK"},
    }
    run_dir = tmp_path / "run"
    summary = runner_mod._execute_routed_runtime(item, run_dir, registry=registry)

    assert summary["routed"] is True
    assert summary["executed"] is True
    assert summary["execution_ok"] is True
    routing_path = run_dir / "loop" / "routing-decision-receipt.json"
    execution_path = run_dir / "loop" / "runtime-execution-receipt.json"
    assert routing_path.exists()
    assert execution_path.exists()

    routing_receipt = json.loads(routing_path.read_text(encoding="utf-8"))
    assert routing_receipt["selected"]["runtime"] == "codex"
    execution_receipt = json.loads(execution_path.read_text(encoding="utf-8"))
    verdict = verify_receipt(execution_receipt, schema=RUNTIME_EXECUTION_RECEIPT_SCHEMA)
    assert verdict.status == ReceiptStatus.VERIFIED


def test_execute_routed_runtime_reports_blocked_when_no_candidate_eligible(tmp_path):
    # No entries at all: routing is genuinely blocked, never fabricated as a selection.
    registry = ModelCapabilityRegistry([])
    item = {"repo": str(tmp_path), "task_id": "task-2", "task_index": 1, "worker_id": "w1",
            "context_pack": {"goal": "do something"}}
    run_dir = tmp_path / "run"
    summary = runner_mod._execute_routed_runtime(item, run_dir, registry=registry)
    assert summary["routed"] is True
    assert summary["blocked"] is True
    assert summary["executed"] is False
    assert summary["block_reason"]


def test_execute_routed_runtime_reports_no_driver_wired_honestly(tmp_path):
    # A runtime this repo's registry can select but for which no real driver exists yet
    # (e.g. any of the other 10 adapters) must say so explicitly, never silently no-op.
    registry = ModelCapabilityRegistry([
        {"runtime": "cursor", "provider": "cursor", "model_id": "cursor-default",
         "capabilities": ["execute"], "probe": {"kind": "binary_on_path", "target": "true"}},
    ])
    item = {"repo": str(tmp_path), "task_id": "task-3", "task_index": 1, "worker_id": "w1",
            "context_pack": {"goal": "do something"}}
    run_dir = tmp_path / "run"
    summary = runner_mod._execute_routed_runtime(item, run_dir, registry=registry)
    assert summary["routed"] is True
    assert summary["executed"] is False
    assert "no real driver wired" in summary["reason"]


def test_operator_dispatch_attempt_wires_model_routing_when_opted_in(tmp_path, monkeypatch):
    """The real dispatch path (``_operator_dispatch_attempt``) must call the routed-runtime
    helper and attach its summary to every returned record when
    ``SIMPLICIO_MODEL_ROUTED_DISPATCH=1`` -- and must NOT call it at all by default."""
    calls = []

    def fake_execute_routed(item, run_dir, *, registry=None):
        calls.append((item["task_id"], str(run_dir)))
        return {"routed": True, "executed": True, "routing_decision_receipt": "x", "runtime_execution_receipt": "y"}

    monkeypatch.setattr(runner_mod, "_execute_routed_runtime", fake_execute_routed)
    monkeypatch.setattr(runner_mod, "read_status", lambda repo, run_id: {"run_dir": str(tmp_path / "run")})
    monkeypatch.setattr(
        runner_mod, "execute_operator",
        lambda repo, run_id, task_index=1, **kwargs: {
            "state": {"operator": {"execution_state": "applied", "receipt": ""},
                      "evidence": {"receipt": ""}, "attempts": 1, "phase": "delivered"},
            "run_dir": str(tmp_path / "run"),
        },
    )

    item = {"repo": str(tmp_path), "run_id": "run-1", "task_index": 1, "worker_id": "w1", "task_id": "task-4"}

    monkeypatch.setenv("SIMPLICIO_MODEL_ROUTED_DISPATCH", "1")
    record = runner_mod._operator_dispatch_attempt(item)
    assert calls == [("task-4", str(tmp_path / "run"))]
    assert record["model_routing"]["executed"] is True

    monkeypatch.delenv("SIMPLICIO_MODEL_ROUTED_DISPATCH", raising=False)
    calls.clear()
    record2 = runner_mod._operator_dispatch_attempt(item)
    assert calls == []
    assert "model_routing" not in record2


if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_model_routed_dispatch_wiring")
