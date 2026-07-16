#!/usr/bin/env python3
"""Windows-aware global GitHub Copilot/VS Code surfaces.

The repository-local ``.github`` and ``.vscode`` files remain the source of truth for
project installs.  A global VS Code/Copilot install must additionally update the
user-level surfaces that Copilot actually reads: ``~/.copilot/skills`` and the user
MCP files.  This module is stdlib-only so the installer and doctor can use it before
optional operator packages are available.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


def copilot_home(home: Optional[Path] = None) -> Path:
    return Path(home or Path.home()) / ".copilot"


def vscode_user_dir(*, home: Optional[Path] = None,
                    env: Optional[Mapping[str, str]] = None) -> Path:
    """Return the user-level VS Code data directory for the current platform.

    ``SIMPLICIO_VSCODE_USER_DIR`` is intentionally supported for tests and for
    portable VS Code profiles.  The normal Windows location is APPDATA/Code/User.
    """
    env = env or os.environ
    override = env.get("SIMPLICIO_VSCODE_USER_DIR")
    if override:
        return Path(override).expanduser()
    home = Path(home or Path.home())
    if sys.platform == "win32":
        return Path(env.get("APPDATA", str(home / "AppData" / "Roaming"))) / "Code" / "User"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Code" / "User"
    return Path(env.get("XDG_CONFIG_HOME", str(home / ".config"))) / "Code" / "User"


def _mcp_command(*, home: Optional[Path] = None,
                 env: Optional[Mapping[str, str]] = None) -> str:
    env = env or os.environ
    explicit = env.get("SIMPLICIO_MCP_COMMAND")
    if explicit:
        return explicit
    found = shutil.which("simplicio")
    if found:
        return found
    home = Path(home or Path.home())
    candidates = [
        home / ".local" / "bin" / "simplicio",
        home / ".local" / "bin" / "simplicio.exe",
        home / ".cargo" / "bin" / "simplicio.exe",
        home / ".cargo" / "bin" / "simplicio",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    # Keep the config portable when the runtime is not installed yet.  Doctor
    # reports the missing command rather than writing a false executable path.
    return "simplicio"


def mcp_server(*, command: str) -> Dict[str, Any]:
    return {
        "type": "stdio",
        "command": command,
        "args": ["serve", "--mcp", "--stdio"],
        "tools": ["*"],
    }


def _merge_mcp(path: Path, top_key: str, *, command: str) -> None:
    data: Dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            # Never destroy an unrelated/broken user file.  The caller/doctor
            # will surface the invalid file and the next repair can replace it.
            return
    servers = data.setdefault(top_key, {})
    if not isinstance(servers, dict):
        return
    current = servers.get("simplicio")
    if not isinstance(current, dict):
        current = {}
    current.update(mcp_server(command=command))
    servers["simplicio"] = current
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _global_instruction() -> str:
    return """# Simplicio-loop global instructions

Use the Simplicio-loop agent skill for multi-step or mutating work. Load and follow
`~/.copilot/skills/simplicio-loop/SKILL.md` and its companion skills in full. Use
the local queue, leases, worktrees, validation, evidence, and GitHub lifecycle
comment coordination; never claim completion without concrete evidence.

Invoke the workflow with `/simplicio-loop <the body of work>`. GitHub comments are
the shared coordination projection when a run has a `source_issue`; local state
remains authoritative and usable when GitHub is offline.
"""


def sync_global_copilot(source: Path, skills: list[str], *, home: Optional[Path] = None,
                        env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Synchronize Copilot skills, instructions, and both user MCP surfaces."""
    env = env or os.environ
    source = Path(source)
    home = Path(home or Path.home())
    c_home = copilot_home(home)
    skills_root = c_home / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    copied = 0
    for name in skills:
        src = source / ".claude" / "skills" / name
        dst = skills_root / name
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
            copied += 1

    instructions = c_home / "instructions" / "simplicio-loop.instructions.md"
    instructions.parent.mkdir(parents=True, exist_ok=True)
    instructions.write_text(_global_instruction(), encoding="utf-8")

    command = _mcp_command(home=home, env=env)
    copilot_mcp = c_home / "mcp-config.json"
    vscode_mcp = vscode_user_dir(home=home, env=env) / "mcp.json"
    _merge_mcp(copilot_mcp, "mcpServers", command=command)
    _merge_mcp(vscode_mcp, "servers", command=command)
    return {
        "skills_root": str(skills_root),
        "instructions": str(instructions),
        "copilot_mcp": str(copilot_mcp),
        "vscode_mcp": str(vscode_mcp),
        "mcp_command": command,
        "skills_copied": str(copied),
    }


def verify_global_copilot(*, home: Optional[Path] = None,
                          env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Return a typed, read-only verification report for the active surfaces."""
    home = Path(home or Path.home())
    c_home = copilot_home(home)
    vscode_mcp = vscode_user_dir(home=home, env=env) / "mcp.json"
    result: Dict[str, Any] = {
        "skills_root": str(c_home / "skills"),
        "skills_present": (c_home / "skills").is_dir(),
        "instructions": str(c_home / "instructions" / "simplicio-loop.instructions.md"),
        "instructions_present": (c_home / "instructions" / "simplicio-loop.instructions.md").is_file(),
        "copilot_mcp": str(c_home / "mcp-config.json"),
        "copilot_mcp_valid": False,
        "vscode_mcp": str(vscode_mcp),
        "vscode_mcp_valid": False,
    }
    for key, path, top in (
        ("copilot_mcp_valid", c_home / "mcp-config.json", "mcpServers"),
        ("vscode_mcp_valid", vscode_mcp, "servers"),
    ):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            result[key] = isinstance(data, dict) and isinstance(data.get(top), dict) and "simplicio" in data[top]
        except (OSError, ValueError):
            result[key] = False
    return result
