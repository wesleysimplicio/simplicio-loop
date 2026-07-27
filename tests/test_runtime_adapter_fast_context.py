from pathlib import Path

import pytest

from simplicio_loop.runtime_adapter import LoopRuntimeAdapter, RuntimeCompatibilityError


def test_standalone_adapter_exposes_fast_context(monkeypatch, tmp_path):
    packet = {"schema": "simplicio.context-packet/v1", "generation": "SFAST001:test"}
    captured = {}

    def fake_request(repo, term, **kwargs):
        captured.update({"repo": repo, "term": term, **kwargs})
        return packet

    monkeypatch.setattr("simplicio_loop.runtime_adapter.request_context", fake_request)
    bridge = LoopRuntimeAdapter(
        run_id="run-1", work_item_id="wi-1", actor="loop@host-a", standalone=True
    )

    assert bridge.context(tmp_path, "LoopRuntimeAdapter", max_bytes=4096) == packet
    assert captured == {"repo": tmp_path, "term": "LoopRuntimeAdapter", "snapshot": None, "max_bytes": 4096, "timeout": 60.0}


def test_bound_adapter_refuses_loop_owned_fast_context():
    bridge = LoopRuntimeAdapter(
        run_id="run-1", work_item_id="wi-1", actor="loop@host-a", transport=object()
    )

    with pytest.raises(RuntimeCompatibilityError, match="Runtime transport"):
        bridge.context(Path("."), "term")