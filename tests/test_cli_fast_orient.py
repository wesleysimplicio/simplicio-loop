from __future__ import annotations

import json

import pytest

from simplicio_loop import cli


class _ReadyFast:
    last_config = None

    def __init__(self, root, *, config):
        self.root = root
        self.config = config
        type(self).last_config = config

    def prepare(self, task):
        return {"status": "READY", "generation": "g1", "context_hash": "ctx",
                "task": task}


def test_orient_prefers_fast_and_emits_bounded_receipt(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "FastLoopIntegration", _ReadyFast)
    assert cli.orient(str(tmp_path), "change app", "on", 1234) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "simplicio.loop-orient/v1"
    assert payload["status"] == "READY"
    assert payload["provider"] == "simplicio-fast"
    assert payload["local_llm"] is False
    assert _ReadyFast.last_config.mode == "required"
    assert _ReadyFast.last_config.max_bytes == 1234
    assert _ReadyFast.last_config.engine == "auto"


def test_orient_exposes_explicit_engine_selection(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "FastLoopIntegration", _ReadyFast)
    assert cli.orient(str(tmp_path), "change app", "on", 1234, "rust") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["fast_engine"] == "rust"
    assert _ReadyFast.last_config.engine == "rust"


def test_explicit_rust_does_not_fallback_to_mapper(tmp_path, monkeypatch, capsys):
    class _UnavailableRust(_ReadyFast):
        def prepare(self, task):
            return {"status": "FALLBACK", "reason": "rust_not_verified"}

    monkeypatch.setattr(cli, "FastLoopIntegration", _UnavailableRust)
    assert cli.orient(str(tmp_path), "change app", "auto", 1234, "rust") == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "BLOCKED"
    assert payload["fallback"] is False
    assert payload["fast_engine"] == "rust"
    assert _UnavailableRust.last_config.mode == "required"


def test_orient_auto_uses_mapper_fallback_with_reason(tmp_path, monkeypatch, capsys):
    class _FallbackFast(_ReadyFast):
        def prepare(self, task):
            return {"status": "FALLBACK", "reason": "doctor_failed"}

    monkeypatch.setattr(cli, "FastLoopIntegration", _FallbackFast)
    monkeypatch.setattr(cli, "_mapper_orient_fallback",
                        lambda root, task: {"status": "READY", "result": {"files": 1}})
    assert cli.orient(str(tmp_path), "change app", "auto", 2000) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "FALLBACK"
    assert payload["provider"] == "simplicio-mapper"
    assert payload["fallback_reason"] == "doctor_failed"
    assert payload["local_llm"] is False


def test_orient_on_fails_closed_when_fast_is_unavailable(tmp_path, monkeypatch, capsys):
    class _UnavailableFast(_ReadyFast):
        def prepare(self, task):
            raise cli.FastIntegrationError("missing_operator")

    monkeypatch.setattr(cli, "FastLoopIntegration", _UnavailableFast)
    assert cli.orient(str(tmp_path), "change app", "on", 2000) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "BLOCKED"
    assert payload["fallback"] is False
    assert payload["fallback_reason"] == "missing_operator"


@pytest.mark.parametrize("mode", ["auto", "on", "off"])
def test_orient_help_exposes_fast_modes(mode):
    assert mode in {"auto", "on", "off"}
