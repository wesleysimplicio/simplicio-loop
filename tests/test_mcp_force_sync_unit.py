"""mcp_force_sync + action_gate MCP force unit tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import mcp_force_sync


def test_mcp_force_sync_writes_env_and_rules(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("SIMPLICIO_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    receipt = mcp_force_sync.sync(do_global=True, target=tmp_path / "repo", register=False)
    assert receipt["ok"] is True
    assert receipt["require_mcp"] is True
    env = (home / ".simplicio" / "loop-env.sh").read_text(encoding="utf-8")
    assert "SIMPLICIO_REQUIRE_MCP=1" in env
    assert "SIMPLICIO_MCP_FORCE=1" in env
    rule = home / ".grok" / "rules" / "simplicio-runtime-mcp-force.md"
    assert rule.is_file()
    assert "simplicio_map" in rule.read_text(encoding="utf-8")
    # project mcp.json
    mcp = tmp_path / "repo" / ".mcp.json"
    assert mcp.is_file()
    data = json.loads(mcp.read_text(encoding="utf-8"))
    assert "simplicio" in data.get("mcpServers", {}) or "simplicio" in data.get("servers", {})


def test_action_gate_mcp_force_blocks_host_read(monkeypatch):
    import action_gate as ag

    monkeypatch.setenv("SIMPLICIO_REQUIRE_MCP", "1")
    monkeypatch.delenv("SIMPLICIO_LOOP_STRICT", raising=False)
    monkeypatch.delenv("SIMPLICIO_MCP_FORCE", raising=False)
    assert ag._mcp_force_enabled() is True
    assert ag._CONTEXT_FLOOD_RE.search("cat agent/foo.py")
    assert ag._CONTEXT_FLOOD_RE.search("rg -n def src/main.py")
    assert not ag._CONTEXT_FLOOD_RE.search("simplicio doctor")
    assert "read" in ag._MCP_BYPASS_READ_TOOLS
    assert "grep" in ag._MCP_BYPASS_READ_TOOLS


def test_action_gate_mcp_force_disabled_by_default(monkeypatch):
    import action_gate as ag

    monkeypatch.delenv("SIMPLICIO_REQUIRE_MCP", raising=False)
    monkeypatch.delenv("SIMPLICIO_MCP_FORCE", raising=False)
    assert ag._mcp_force_enabled() is False


def test_mcp_force_sync_idempotent_codex_toml(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("SIMPLICIO_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    r1 = mcp_force_sync.sync(do_global=True, target=None, register=False)
    r2 = mcp_force_sync.sync(do_global=True, target=None, register=False)
    assert r1["ok"] and r2["ok"]
    toml = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert toml.count("[mcp_servers.simplicio]") == 1
