from dataclasses import replace

import pytest

from simplicio_loop.runtime_bridge import RuntimeBridgeRecoveryUnknown
from simplicio_loop.runtime_effect_adapter import EffectRequest, RuntimeEffectAdapter


class FakeBridge:
    def execute(self, workspace, argv, **kwargs):
        return {"success": True, "workspace": workspace, "argv": argv, "idempotency_key": kwargs["idempotency_key"]}

    def runtime_call(self, workspace, tool, arguments, **kwargs):
        return {"success": True, "workspace": workspace, "tool": tool, "arguments": dict(arguments)}


def request(**overrides):
    values = {
        "workspace": "C:/repo",
        "idempotency_key": "run-1:task-1:attempt-1",
        "write_set": ("repo:src",),
        "lease_id": "lease-1",
        "fencing_token": 7,
        "attempt": 2,
        "gate_id": "gate-1",
        "runtime_generation": "generation-3",
        "transaction_id": "tx-1",
    }
    values.update(overrides)
    return EffectRequest(**values)


def test_runtime_effect_receipt_binds_transaction_identity_and_action_digest():
    receipt = RuntimeEffectAdapter(profile="runtime-backed", bridge=FakeBridge()).execute(
        request(), ["pytest", "-q"], env={"SIMPLICIO_MODEL": "local"},
    )
    assert receipt["status"] == "MEASURED"
    assert receipt["transaction"]["schema"] == "simplicio.effect-transaction/v1"
    assert receipt["transaction"]["lease"] == {"id": "lease-1", "fence": 7}
    assert receipt["transaction"]["attempt"] == 2
    assert receipt["transaction"]["idempotency"]["transaction_id"] == "tx-1"
    assert receipt["transaction"]["idempotency"]["action_digest"].startswith("sha256:")
    assert receipt["correlation_id"] == "tx-1"


def test_explicit_effect_methods_use_allowlisted_runtime_tools():
    receipt = RuntimeEffectAdapter(profile="runtime-backed", bridge=FakeBridge()).edit(
        request(), {"path": "src/main.py"},
    )
    assert receipt["result"]["tool"] == "simplicio_edit"
    assert receipt["delivery"] == "RUNTIME"


def test_standalone_effects_are_explicitly_unavailable_and_deterministic():
    receipt = RuntimeEffectAdapter(profile="standalone").evidence(request(), {"status": "PASS"})
    assert receipt["status"] == "UNAVAILABLE"
    assert receipt["delivery"] == "STANDALONE"
    assert receipt["transaction"]["executor"] == "STANDALONE"
    assert receipt["correlation_id"] == "tx-1"


class FailureBridge:
    def __init__(self, failure):
        self.failure = failure
        self.calls = []

    def execute(self, workspace, argv, **kwargs):
        self.calls.append(("execute", kwargs["idempotency_key"]))
        raise self.failure

    def runtime_call(self, workspace, tool, arguments, **kwargs):
        self.calls.append((tool, dict(arguments)))
        if tool == "simplicio_reconcile":
            return {
                "status": "MEASURED",
                "outcome": "COMMITTED",
                "process_count": 1,
                "tokens": 0,
                "rss_bytes": 4096,
            }
        raise self.failure


def test_uncertain_effect_requires_reconciliation_and_is_never_replayed():
    bridge = FailureBridge(RuntimeBridgeRecoveryUnknown("disconnect after effect"))
    adapter = RuntimeEffectAdapter(profile="runtime-backed", bridge=bridge)
    uncertain = adapter.execute(request(), ["python", "-V"])
    assert uncertain["status"] == "UNCERTAIN"
    assert uncertain["delivery"] == "RUNTIME_RECONCILE_REQUIRED"
    assert uncertain["result"]["safe_to_replay"] is False
    assert bridge.calls == [("execute", "run-1:task-1:attempt-1")]

    reconciled = adapter.reconcile(request())
    assert reconciled["status"] == "MEASURED"
    assert reconciled["result"]["outcome"] == "COMMITTED"
    assert bridge.calls[1][0] == "simplicio_reconcile"
    assert bridge.calls[1][1]["replay"] is False


def test_runtime_missing_or_crash_never_downgrades_to_standalone():
    bridge = FailureBridge(FileNotFoundError("simplicio missing"))
    receipt = RuntimeEffectAdapter(profile="runtime-backed", bridge=bridge).execute(
        request(), ["python", "-V"]
    )
    assert receipt["status"] == "UNAVAILABLE"
    assert receipt["delivery"] == "RUNTIME_UNAVAILABLE"
    assert receipt["profile"] == "runtime-backed"
    assert receipt["executor"] == "simplicio-runtime"


def test_metrics_publish_latency_and_null_reasons_or_runtime_values():
    bridge = FailureBridge(RuntimeBridgeRecoveryUnknown("uncertain"))
    adapter = RuntimeEffectAdapter(profile="runtime-backed", bridge=bridge)
    first = adapter.execute(request(), ["python", "-V"])
    assert first["metrics"]["latency"]["p50_ms"] is not None
    assert first["metrics"]["tokens"] is None
    assert first["metrics"]["tokens_reason"] == "runtime_not_reported"

    measured = adapter.reconcile(request())
    assert measured["metrics"]["process_count"] == 1
    assert measured["metrics"]["tokens"] == 0
    assert measured["metrics"]["rss_bytes"] == 4096


def test_all_production_methods_route_only_to_their_allowlisted_runtime_tools():
    bridge = FakeBridge()
    adapter = RuntimeEffectAdapter(profile="runtime-backed", bridge=bridge)
    expected = {
        "map": "simplicio_map",
        "read": "simplicio_read",
        "edit": "simplicio_edit",
        "validate": "simplicio_validate",
        "checkpoint": "simplicio_checkpoint",
        "evidence": "simplicio_evidence",
    }
    for method, tool in expected.items():
        receipt = getattr(adapter, method)(request(), {"probe": method})
        assert receipt["result"]["tool"] == tool


def test_invalid_effect_surfaces_fail_before_runtime():
    adapter = RuntimeEffectAdapter(profile="runtime-backed", bridge=FakeBridge())
    with pytest.raises(Exception, match="argv"):
        adapter.execute(request(), [])
    with pytest.raises(Exception, match="allowlisted"):
        adapter.call(request(), "subprocess", {})
    with pytest.raises(Exception, match="allowlisted"):
        adapter.call(request(), "simplicio_bad/tool", {})
    with pytest.raises(Exception, match="object"):
        adapter.call(request(), "simplicio_status", [])  # type: ignore[arg-type]
    with pytest.raises(Exception, match="profile"):
        RuntimeEffectAdapter(profile="automatic")  # type: ignore[arg-type]


def test_runtime_call_failure_is_unavailable_without_fallback():
    bridge = FailureBridge(ConnectionError("runtime crashed"))
    receipt = RuntimeEffectAdapter(profile="runtime-backed", bridge=bridge).call(
        request(), "simplicio_status", {}
    )
    assert receipt["status"] == "UNAVAILABLE"
    assert receipt["delivery"] == "RUNTIME_UNAVAILABLE"


def test_authorization_digest_is_bound_to_transaction_and_receipt():
    digest = "sha256:" + "a" * 64
    req = replace(request(), authorization_digest=digest)
    receipt = RuntimeEffectAdapter(profile="runtime-backed", bridge=FakeBridge()).execute(
        req, ["python", "-V"]
    )
    assert receipt["authorization_digest"] == digest
    assert receipt["transaction"]["authorization_digest"] == digest


def test_authorization_digest_rejects_non_sha256_values():
    with pytest.raises(Exception, match="authorization_digest"):
        replace(request(), authorization_digest="not-a-digest")
