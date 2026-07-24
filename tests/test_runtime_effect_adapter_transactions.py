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
