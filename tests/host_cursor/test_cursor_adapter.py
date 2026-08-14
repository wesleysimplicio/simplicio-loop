import pytest

from adapters.cursor.adapter import (
    AdapterError,
    capabilities,
    decide,
    detect,
    verify_shipped_hooks,
)
from pathlib import Path

ADAPTER = Path(__file__).resolve().parents[2] / "adapters" / "cursor"


def test_detects_cursor_workspace(tmp_path: Path):
    (tmp_path / ".cursor-plugin").mkdir()
    (tmp_path / ".cursor-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    assert detect({"CURSOR": "1"}, tmp_path)["detected"] is True
    assert detect({}, tmp_path / "empty")["detected"] is False


def test_claimed_hooks_fail_closed(tmp_path: Path):
    with pytest.raises(AdapterError, match="claimed Cursor hook missing"):
        verify_shipped_hooks(tmp_path)
    assert verify_shipped_hooks(ADAPTER)["verified"] is True


def test_capability_matrix_is_honest_about_t3_gap():
    matrix = capabilities()
    assert matrix["claude_parity"]["PreToolUse.shell"] == "native"
    assert matrix["claude_parity"]["PreToolUse.edit"] == "unsupported_native"
    assert matrix["stages"]["PreToolUse.edit"]["supported"] is False
    assert matrix["stages"]["PreToolUse.edit"]["enforcement"] == "self_paced"
    for stage in ("Stop", "PostToolUse", "PreToolUse.shell"):
        hook = matrix["stages"][stage]["hook"]
        assert (ADAPTER / hook).is_file()


def test_t4_shell_blocks_mutating_commands():
    blocked = decide({
        "hook_event_name": "beforeShellExecution",
        "command": "git push --force origin main",
    })
    assert blocked["decision"] == "block"
    allowed = decide({"hook_event_name": "beforeShellExecution", "command": "git status"})
    assert allowed["decision"] == "continue"


def test_missing_t3_is_self_paced_not_green_native():
    decision = decide({"hook_event_name": "beforeEdit", "path": "app.py"})
    assert decision["native"] is False
    assert decision["enforcement"] == "self_paced"
    assert decide({"hook_event_name": "PreToolUse.edit"})["enforcement"] == "self_paced"


def test_stop_and_timeout():
    assert decide({"hook_event_name": "stop", "evidence_complete": False})["decision"] == "refeed"
    assert decide({"hook_event_name": "stop"}, timeout=True)["reason"] == "hook_timeout_does_not_authorize"
