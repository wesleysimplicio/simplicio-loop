import json
from pathlib import Path

from adapters.codex.adapter import (
    capabilities,
    decide,
    detect,
    diagnose,
    verify_shipped_hooks,
    watcher_state,
)

REPO = Path(__file__).resolve().parents[2]


def test_detects_codex_workspace(tmp_path: Path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text("hooks = true\n", encoding="utf-8")
    assert detect({"CODEX": "1"}, tmp_path)["detected"] is True
    assert detect({}, tmp_path / "empty")["detected"] is False


def test_empty_hooks_json_is_not_native_interception():
    inventory = capabilities(REPO)
    assert inventory["native_interception"] is False
    assert inventory["governed_effect_path"] is True
    assert inventory["mcp_required"] is True
    assert inventory["hooks_inventory"]["empty"] is True
    assert inventory["stages"]["PreToolUse"]["supported"] is False
    assert inventory["raw_shell_write"] == "unenforceable"


def test_does_not_claim_native_hooks():
    assert verify_shipped_hooks()["claimed_native"] == []
    report = diagnose(REPO)
    assert report["native_interception"] is False
    assert report["status"] == "MEASURED_HOOKS_EMPTY"


def test_raw_write_without_mcp_is_blocked_not_green():
    blocked = decide({"stage": "PreToolUse", "kind": "Write", "via_mcp": False})
    assert blocked["decision"] == "block"
    assert "unenforceable" in blocked["reason"] or blocked["reason"] == "native_pretooluse_absent"
    raw = decide({"kind": "shell", "via_mcp": False, "stage": "tool"})
    assert raw["decision"] == "block"
    mcp = decide({"stage": "PostToolUse", "kind": "Write", "via_mcp": True})
    assert mcp["decision"] == "continue"


def test_self_paced_watcher_survives_reload(tmp_path: Path):
    store = tmp_path / "watcher.json"
    first = watcher_state(store, {"id": "turn-1"})
    assert first["durable"] is True
    assert first["fire_and_forget"] is False
    second = watcher_state(store, {"id": "turn-2"})
    assert [item["id"] for item in second["queue"]] == ["turn-1", "turn-2"]
    reloaded = json.loads(store.read_text(encoding="utf-8"))
    assert len(reloaded["queue"]) == 2
