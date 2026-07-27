import hashlib
import json
import subprocess

import pytest

from simplicio_loop.context_provider import (
    ContextProviderError,
    request_context,
)


def _snapshot(tmp_path):
    path = tmp_path / ".simplicio" / "fast" / "project.sfast"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"snapshot")
    return path


def _fast_response():
    return {
        "schema": "simplicio.fast.context/v1",
        "provenance": {
            "schema": "simplicio.fast.provenance/v1",
            "snapshot_generation": "SFAST001:generation",
            "snapshot_sha256": "abc123",
        },
        "spans": [{"file": "simplicio_loop/runtime_adapter.py", "content": "class LoopRuntimeAdapter: ..."}],
    }


def test_request_context_emits_agent_packet_without_local_model(monkeypatch, tmp_path):
    snapshot = _snapshot(tmp_path)
    response = _fast_response()
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        assert all("llama" not in part.casefold() and "ollama" not in part.casefold() for part in command)
        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

    monkeypatch.setattr("simplicio_loop.context_provider.subprocess.run", fake_run)
    packet = request_context(tmp_path, "LoopRuntimeAdapter", snapshot=snapshot, max_bytes=4096, fast_bin="simplicio-fast")

    assert packet["schema"] == "simplicio.context-packet/v1"
    assert packet["provenance"]["provider"] == "simplicio-fast"
    assert packet["provenance"]["local_llm_started"] is False
    assert packet["source"] == "loop-fast"
    payload = {key: packet[key] for key in ("generation", "spans", "provenance", "fidelity", "complete", "source")}
    expected = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert packet["content_sha256"] == expected
    assert captured["kwargs"]["cwd"] == str(tmp_path.resolve())
    assert "--max-bytes" in captured["command"] and "4096" in captured["command"]


def test_request_context_rejects_stale_provider_output(monkeypatch, tmp_path):
    snapshot = _snapshot(tmp_path)
    response = _fast_response()
    response["schema"] = "simplicio.fast.context/v0"
    monkeypatch.setattr(
        "simplicio_loop.context_provider.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, json.dumps(response), ""),
    )

    with pytest.raises(ContextProviderError, match="unexpected context schema"):
        request_context(tmp_path, "term", snapshot=snapshot, fast_bin="simplicio-fast")


def test_request_context_rejects_snapshot_outside_simplicio(tmp_path):
    outside = tmp_path / "outside.sfast"
    outside.write_bytes(b"snapshot")

    with pytest.raises(ContextProviderError, match="under <repo>/.simplicio"):
        request_context(tmp_path, "term", snapshot=outside, fast_bin="simplicio-fast")