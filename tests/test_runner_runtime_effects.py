"""Focused production-runner coverage for RuntimeEffectAdapter dispatch."""

from __future__ import annotations

import json

import pytest

from simplicio_loop import runner
from simplicio_loop.hookwall_gate import HookwallBlocked, gate_completion
from simplicio_loop.runtime_effect_adapter import EffectRequest, RuntimeEffectAdapter, RuntimeEffectError


class FakeBridge:
    def __init__(self) -> None:
        self.calls = []

    def execute(self, workspace, argv, **kwargs):
        self.calls.append((workspace, list(argv), kwargs))
        return {
            "status": "MEASURED",
            "returncode": 0,
            "stdout": json.dumps({"applied": True}),
            "stderr": "",
            "runtime_generation": "runtime-gen-7",
        }


def _request(tmp_path):
    return EffectRequest(
        workspace=str(tmp_path),
        idempotency_key="run-695:task-1:1",
        write_set=("repo:simplicio_loop/runner.py",),
        lease_id="lease-695",
        fencing_token=7,
        attempt=1,
        gate_id="gate-695",
        runtime_generation="runtime-gen-7",
        transaction_id="tx-695",
    )


def test_execution_profile_is_validated_and_rejects_unknown(monkeypatch):
    monkeypatch.setenv("SIMPLICIO_EXECUTION_PROFILE", "runtime-backed")
    assert runner._execution_profile() == "runtime-backed"
    monkeypatch.setenv("SIMPLICIO_EXECUTION_PROFILE", "unexpected")
    with pytest.raises(
        RuntimeEffectError,
        match="standalone, runtime-backed, or auto",
    ):
        runner._execution_profile()


def test_runtime_backed_effect_never_uses_direct_or_fake_mutation(tmp_path, monkeypatch):
    bridge = FakeBridge()
    adapter = RuntimeEffectAdapter(profile="runtime-backed", bridge=bridge)
    monkeypatch.setenv(
        "SIMPLICIO_LOOP_FAKE_OPERATOR_EXEC_JSON",
        json.dumps({"write_files": {"bypassed.txt": "must-not-write"}}),
    )
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("direct subprocess bypass")),
    )
    outcome = runner._execute_operator_effect(
        profile="runtime-backed",
        adapter=adapter,
        request=_request(tmp_path),
        argv=["simplicio-dev-cli", "task", "compile"],
        env={},
        repo_path=tmp_path,
        attempt_coordinator=None,
        guarded_attempt=None,
    )
    assert outcome["returncode"] == 0
    assert outcome["source"] == "runtime_effect_adapter"
    assert not (tmp_path / "bypassed.txt").exists()
    assert len(bridge.calls) == 1
    assert gate_completion(outcome["hookwall_evidence"]) == (True, "ok")
    assert outcome["hookwall_mutation_receipt"]["status"] == "committed"


def test_runtime_receipt_correlates_transaction_identity(tmp_path):
    bridge = FakeBridge()
    receipt = RuntimeEffectAdapter(profile="runtime-backed", bridge=bridge).execute(
        _request(tmp_path), ["simplicio-dev-cli", "task", "compile"], env={},
    )
    assert receipt["profile"] == "runtime-backed"
    assert receipt["executor_profile"] == "runtime-backed"
    assert receipt["executor"] == "simplicio-runtime"
    assert receipt["transaction_id"] == "tx-695"
    assert receipt["correlation_id"] == "tx-695"
    assert receipt["transaction"]["lease"] == {"id": "lease-695", "fence": 7}
    assert receipt["transaction"]["gate"]["id"] == "gate-695"
    assert receipt["transaction"]["runtime_generation"] == "runtime-gen-7"


def test_standalone_fake_path_remains_functional(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SIMPLICIO_LOOP_FAKE_OPERATOR_EXEC_JSON",
        json.dumps({"write_files": {"standalone.txt": "preserved"}, "stdout": {"ok": True}}),
    )
    outcome = runner._execute_operator_effect(
        profile="standalone",
        adapter=RuntimeEffectAdapter(profile="standalone"),
        request=_request(tmp_path),
        argv=["simplicio-dev-cli", "task", "compile"],
        env={},
        repo_path=tmp_path,
        attempt_coordinator=None,
        guarded_attempt=None,
    )
    assert outcome["source"] == "env_override"
    assert (tmp_path / "standalone.txt").read_text(encoding="utf-8") == "preserved"
    assert gate_completion(outcome["hookwall_evidence"]) == (True, "ok")


def test_hookwall_pre_blocks_before_any_operator_effect(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(
        runner,
        "validate_pre_decision",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            HookwallBlocked("hookwall_pre_blocked", "policy denied")
        ),
    )
    monkeypatch.setattr(
        runner,
        "_execute_operator_effect_unchecked",
        lambda **kwargs: called.append(kwargs),
    )
    with pytest.raises(HookwallBlocked, match="hookwall_pre_blocked"):
        runner._execute_operator_effect(
            profile="standalone",
            adapter=RuntimeEffectAdapter(profile="standalone"),
            request=_request(tmp_path),
            argv=["simplicio-dev-cli", "task", "compile"],
            env={},
            repo_path=tmp_path,
            attempt_coordinator=None,
            guarded_attempt=None,
        )
    assert called == []



def test_hookwall_completion_rejects_missing_and_unverified_evidence():
    assert gate_completion(None) == (False, "hookwall_evidence_missing")
    evidence = {
        "schema": "simplicio.hookwall-evidence/v1",
        "envelope_id": "tx",
        "envelope_hash": "env",
        "receipt_hash": "receipt",
        "idempotency_key": "key",
        "fence": 1,
        "verdict": "blocked",
    }
    evidence["evidence_hash"] = runner._hookwall_digest(evidence)
    assert gate_completion(evidence) == (False, "hookwall_effect_unverified")
