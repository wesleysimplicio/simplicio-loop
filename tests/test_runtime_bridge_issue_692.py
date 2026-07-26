import os

from simplicio_loop.runtime_bridge import RuntimeBridge


def test_unspecified_runtime_does_not_force_legacy_alias(monkeypatch):
    monkeypatch.delenv("SIMPLICIO_RUNTIME_BIN", raising=False)
    bridge = RuntimeBridge()
    assert bridge.binary is None


def test_explicit_runtime_selection_remains_authoritative(monkeypatch):
    monkeypatch.setenv("SIMPLICIO_RUNTIME_BIN", os.path.join("tmp", "selected-simplicio"))
    bridge = RuntimeBridge()
    assert bridge.binary.endswith("selected-simplicio")
