import pytest

from simplicio_loop.runtime_effect_adapter import EffectRequest, RuntimeEffectAdapter, RuntimeEffectError


def request():
    return EffectRequest("C:/repo", "run:task:attempt", ("repo:src",), "lease-1", 3)


class FakeBridge:
    def execute(self, *args, **kwargs):
        return {"ok": True, "argv": args[1]}

    def runtime_call(self, *args, **kwargs):
        return {"ok": True, "tool": args[1]}


def test_runtime_profile_routes_through_bridge_and_preserves_authority():
    adapter = RuntimeEffectAdapter(profile="runtime-backed", bridge=FakeBridge())
    receipt = adapter.execute(request(), ["pytest", "-q"])
    assert receipt["executor"] == "simplicio-runtime"
    assert receipt["lease_id"] == "lease-1"
    assert receipt["fencing_token"] == 3
    assert receipt["result"]["ok"] is True


def test_standalone_profile_is_explicit_and_never_fakes_runtime_delivery():
    receipt = RuntimeEffectAdapter(profile="standalone").call(request(), "simplicio_status", {})
    assert receipt["status"] == "UNAVAILABLE"
    assert receipt["result"]["reason"] == "standalone_profile"


def test_runtime_profile_requires_bridge_and_effect_identity():
    with pytest.raises(RuntimeEffectError, match="RuntimeBridge"):
        RuntimeEffectAdapter(profile="runtime-backed")
    with pytest.raises(RuntimeEffectError, match="lease"):
        EffectRequest("C:/repo", "key", ("repo:src",), "", 0)
