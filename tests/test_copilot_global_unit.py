"""#415: prove the active user-level Copilot/VS Code install surfaces."""
import json
from pathlib import Path

from scripts.copilot_global import sync_global_copilot, verify_global_copilot, vscode_user_dir


SKILLS = ["simplicio-loop", "simplicio-tasks"]


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    for name in SKILLS:
        skill = source / ".claude" / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: %s\n---\nlatest\n" % name,
                                          encoding="utf-8")
    return source


def test_global_sync_targets_copilot_and_vscode_user_surfaces(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vscode = tmp_path / "AppData" / "Roaming" / "Code" / "User"
    vscode.mkdir(parents=True)
    (vscode / "mcp.json").write_text(json.dumps({"servers": {"other": {"command": "keep"}}}),
                                      encoding="utf-8")
    monkeypatch.setenv("SIMPLICIO_VSCODE_USER_DIR", str(vscode))
    monkeypatch.setenv("SIMPLICIO_MCP_COMMAND", str(tmp_path / "simplicio.exe"))

    result = sync_global_copilot(_source(tmp_path), SKILLS, home=home)

    assert result["skills_copied"] == "2"
    assert (home / ".copilot" / "skills" / "simplicio-loop" / "SKILL.md").is_file()
    assert (home / ".copilot" / "instructions" / "simplicio-loop.instructions.md").is_file()
    copilot_mcp = json.loads((home / ".copilot" / "mcp-config.json").read_text())
    vscode_mcp = json.loads((vscode / "mcp.json").read_text())
    assert copilot_mcp["mcpServers"]["simplicio"]["command"].endswith("simplicio.exe")
    assert vscode_mcp["servers"]["other"]["command"] == "keep"
    assert vscode_mcp["servers"]["simplicio"]["args"] == ["serve", "--mcp", "--stdio"]

    report = verify_global_copilot(home=home, env={"SIMPLICIO_VSCODE_USER_DIR": str(vscode)})
    assert all(report[key] for key in ("skills_present", "instructions_present",
                                       "copilot_mcp_valid", "vscode_mcp_valid"))


def test_vscode_user_dir_honors_portable_profile_override(tmp_path):
    expected = tmp_path / "portable" / "User"
    assert vscode_user_dir(home=tmp_path, env={"SIMPLICIO_VSCODE_USER_DIR": str(expected)}) == expected


def test_global_sync_is_idempotent_and_refreshes_skill_content(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vscode = tmp_path / "vscode"
    monkeypatch.setenv("SIMPLICIO_VSCODE_USER_DIR", str(vscode))
    source = _source(tmp_path)
    sync_global_copilot(source, SKILLS, home=home)
    (source / ".claude" / "skills" / "simplicio-loop" / "SKILL.md").write_text("refreshed\n",
                                                                                     encoding="utf-8")
    sync_global_copilot(source, SKILLS, home=home)
    assert (home / ".copilot" / "skills" / "simplicio-loop" / "SKILL.md").read_text() == "refreshed\n"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
