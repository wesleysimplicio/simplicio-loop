from pathlib import Path

from adapters.vscode.adapter import capabilities, decide, detect, doctor, verify_shipped_hooks


def test_detects_vscode_workspace(tmp_path: Path):
    (tmp_path / ".vscode").mkdir()
    (tmp_path / ".vscode" / "mcp.json").write_text("{}", encoding="utf-8")
    found = detect({"VSCODE_PID": "1"}, tmp_path)
    assert found["detected"] is True
    assert detect({}, tmp_path / "empty")["detected"] is False


def test_no_claimed_native_hooks():
    assert verify_shipped_hooks()["claimed_native"] == []
    matrix = capabilities()
    assert matrix["native_interception"] is False
    assert matrix["stages"]["PreToolUse"]["supported"] is False
    assert matrix["residual_unmanaged_mutation"] is True


def test_unmanaged_mutation_is_residual_not_false_pass():
    decision = decide({"stage": "PostToolUse", "unmanaged_mutation": True})
    assert decision["decision"] == "block"
    assert decision["residual"] is True
    assert decision["false_pass"] is False


def test_mcp_required_for_writes():
    assert decide({"kind": "Write", "via_mcp": False})["decision"] == "block"
    assert decide({"stage": "PostToolUse", "kind": "Write", "via_mcp": True})["decision"] == "continue"


def test_doctor_bound_vs_unbound(tmp_path: Path):
    assert doctor(tmp_path)["state"] == "unbound"
    (tmp_path / ".vscode").mkdir()
    (tmp_path / ".vscode" / "mcp.json").write_text("{}", encoding="utf-8")
    assert doctor(tmp_path)["state"] == "bound"
