#!/usr/bin/env python3
"""Force-wire Simplicio Runtime MCP for every LLM host + sync FORCE rules.

1. Writes SIMPLICIO_REQUIRE_MCP / SIMPLICIO_MCP_FORCE into ~/.simplicio/loop-env.*
2. Copies packaging/host-rules/simplicio-runtime-mcp-force.md to host rule surfaces
3. Best-effort: runs `simplicio mcp register` and merges common MCP config files
4. Idempotent merge for Claude/Cursor/VS Code/Codex/Kiro JSON-TOML snippets

Usage:
  python3 scripts/mcp_force_sync.py --global --json
  python3 scripts/mcp_force_sync.py --global --target . --json
  python3 scripts/mcp_force_sync.py --register-only
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent
RULE_SRC = SOURCE / "packaging" / "host-rules" / "simplicio-runtime-mcp-force.md"
RULE_NAME = "simplicio-runtime-mcp-force.md"
OWNED = ("FORCE Simplicio Runtime MCP", "SIMPLICIO_REQUIRE_MCP", "simplicio_map")

MCP_SERVER_STDIO = {
    "command": "simplicio",
    "args": ["serve", "--mcp", "--stdio"],
}


def _home() -> Path:
    return Path(os.environ.get("SIMPLICIO_HOME") or os.environ.get("HOME") or Path.home())


def _which_simplicio() -> str | None:
    return shutil.which("simplicio")


def _write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return str(path)


def _merge_json_mcp(path: Path, *, servers_key: str = "mcpServers") -> str:
    """Merge simplicio stdio server into a JSON MCP config."""
    data: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            return f"skip-invalid:{path}"
    bucket = data.setdefault(servers_key, {})
    if not isinstance(bucket, dict):
        data[servers_key] = {}
        bucket = data[servers_key]
    # VS Code uses "servers" not mcpServers
    if servers_key == "servers" and "mcpServers" in data and isinstance(data["mcpServers"], dict):
        bucket = data["mcpServers"]
        servers_key = "mcpServers"
        data.setdefault("servers", {})
    entry = dict(MCP_SERVER_STDIO)
    # Prefer absolute binary when available
    binary = _which_simplicio()
    if binary:
        entry["command"] = binary
    bucket["simplicio"] = entry
    # Also write dual key for VS Code-style
    if servers_key == "mcpServers":
        data.setdefault("servers", {})
        if isinstance(data["servers"], dict):
            data["servers"]["simplicio"] = entry
    _write(path, json.dumps(data, indent=2) + "\n")
    return str(path)


def _merge_codex_toml(path: Path) -> str:
    binary = _which_simplicio() or "simplicio"
    block = (
        "\n# simplicio-runtime MCP (mcp_force_sync)\n"
        "[mcp_servers.simplicio]\n"
        f'command = "{binary.replace(chr(92), "/")}"\n'
        'args = ["serve", "--mcp", "--stdio"]\n'
    )
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if "[mcp_servers.simplicio]" in existing:
        return f"already:{path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(existing.rstrip() + "\n" + block, encoding="utf-8", newline="\n")
    return str(path)


def _patch_env_file(path: Path, shell: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if shell == "ps1":
        lines = [
            '# Simplicio loop + MCP force (mcp_force_sync.py)',
            '$env:SIMPLICIO_LOOP = "1"',
            '$env:SIMPLICIO_LOOP_STRICT = "1"',
            '$env:SIMPLICIO_LOOP_REQUIRE_RUNTIME = "auto"',
            '$env:SIMPLICIO_REQUIRE_MUTATION_AUTHORITY = "1"',
            '$env:SIMPLICIO_LOOP_AUTO_PLANNING_RECEIPT = "1"',
            '$env:SIMPLICIO_LOOP_FORBID_HAND_EDIT = "1"',
            '$env:SIMPLICIO_EXECUTION_PROFILE = "runtime-backed"',
            '$env:SIMPLICIO_FAST_MODE = "required"',
            '$env:SIMPLICIO_REQUIRE_MCP = "1"',
            '$env:SIMPLICIO_MCP_FORCE = "1"',
            "",
        ]
        text = "\n".join(lines)
    else:
        text = """# Simplicio loop + MCP force (mcp_force_sync.py)
export SIMPLICIO_LOOP=1
export SIMPLICIO_LOOP_STRICT=1
export SIMPLICIO_LOOP_REQUIRE_RUNTIME=auto
export SIMPLICIO_REQUIRE_MUTATION_AUTHORITY=1
export SIMPLICIO_LOOP_AUTO_PLANNING_RECEIPT=1
export SIMPLICIO_LOOP_FORBID_HAND_EDIT=1
export SIMPLICIO_EXECUTION_PROFILE=runtime-backed
export SIMPLICIO_FAST_MODE=required
export SIMPLICIO_REQUIRE_MCP=1
export SIMPLICIO_MCP_FORCE=1
"""
    return _write(path, text)


def rule_destinations(home: Path, target: Path | None) -> list[tuple[str, Path]]:
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    dests = [
        ("claude_rules", home / ".claude" / "rules" / RULE_NAME),
        ("grok", home / ".grok" / "rules" / RULE_NAME),
        ("codex", home / ".codex" / "rules" / RULE_NAME),
        ("agents", home / ".agents" / "rules" / RULE_NAME),
        ("cursor_user", home / ".cursor" / "rules" / RULE_NAME),
        ("vscode_skills", home / ".vscode" / "simplicio-skills" / "rules" / RULE_NAME),
        ("vscode_user", appdata / "Code" / "User" / "simplicio-rules" / RULE_NAME),
        ("kiro_user", home / ".kiro" / "steering" / RULE_NAME),
        ("hermes", home / ".hermes" / "rules" / RULE_NAME),
        ("simplicio_agent", home / ".simplicio" / "rules" / RULE_NAME),
        ("antigravity", home / ".antigravity" / "rules" / RULE_NAME),
    ]
    if target is not None:
        root = target.resolve()
        dests.extend(
            [
                ("project_claude", root / ".claude" / "rules" / RULE_NAME),
                ("project_cursor", root / ".cursor" / "rules" / RULE_NAME),
                ("project_mcp_json", root / ".mcp.json"),
                ("project_cursor_mcp", root / ".cursor" / "mcp.json"),
                ("project_vscode_mcp", root / ".vscode" / "mcp.json"),
                ("project_kiro_mcp", root / ".kiro" / "settings" / "mcp.json"),
            ]
        )
    return dests


def register_runtime() -> dict:
    binary = _which_simplicio()
    if not binary:
        return {"ok": False, "error": "simplicio binary not on PATH"}
    try:
        proc = subprocess.run(
            [binary, "mcp", "register"],
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
        )
        return {
            "ok": proc.returncode == 0,
            "exit": proc.returncode,
            "stdout": (proc.stdout or "")[-2000:],
            "stderr": (proc.stderr or "")[-1000:],
            "binary": binary,
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc), "binary": binary}


def sync(*, do_global: bool, target: Path | None, register: bool) -> dict:
    if not RULE_SRC.is_file():
        raise FileNotFoundError(str(RULE_SRC))
    body = RULE_SRC.read_text(encoding="utf-8")
    home = _home()
    written: list[dict] = []

    if do_global:
        written.append(
            {
                "surface": "env_ps1",
                "path": _patch_env_file(home / ".simplicio" / "loop-env.ps1", "ps1"),
                "kind": "env",
            }
        )
        written.append(
            {
                "surface": "env_sh",
                "path": _patch_env_file(home / ".simplicio" / "loop-env.sh", "sh"),
                "kind": "env",
            }
        )
        # User-level MCP configs
        for name, path, key in (
            ("claude_json", home / ".claude.json", "mcpServers"),
            ("cursor_mcp", home / ".cursor" / "mcp.json", "mcpServers"),
            (
                "vscode_mcp",
                Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
                / "Code"
                / "User"
                / "mcp.json",
                "servers",
            ),
            ("kiro_mcp", home / ".kiro" / "settings" / "mcp.json", "mcpServers"),
        ):
            try:
                written.append(
                    {
                        "surface": name,
                        "path": _merge_json_mcp(path, servers_key=key),
                        "kind": "mcp-json",
                    }
                )
            except OSError as exc:
                written.append({"surface": name, "path": str(path), "kind": "error", "error": str(exc)})
        try:
            written.append(
                {
                    "surface": "codex_toml",
                    "path": _merge_codex_toml(home / ".codex" / "config.toml"),
                    "kind": "mcp-toml",
                }
            )
        except OSError as exc:
            written.append({"surface": "codex_toml", "error": str(exc), "kind": "error"})

    for name, path in rule_destinations(home if do_global else _home(), target):
        if path.name in {"mcp.json", ".mcp.json"}:
            key = "servers" if ".vscode" in str(path) else "mcpServers"
            try:
                written.append(
                    {
                        "surface": name,
                        "path": _merge_json_mcp(path, servers_key=key),
                        "kind": "mcp-json",
                    }
                )
            except OSError as exc:
                written.append({"surface": name, "error": str(exc), "kind": "error"})
            continue
        if path.suffix == ".md" or "rules" in path.parts or "steering" in path.parts:
            written.append({"surface": name, "path": _write(path, body), "kind": "rule"})

    reg = register_runtime() if register else {"ok": None, "skipped": True}
    return {
        "schema": "simplicio.mcp-force-sync/v1",
        "ok": True,
        "require_mcp": True,
        "written": written,
        "count": len(written),
        "register": reg,
        "simplicio_binary": _which_simplicio(),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--global", dest="do_global", action="store_true")
    p.add_argument("--target", type=str, default=None)
    p.add_argument("--register-only", action="store_true")
    p.add_argument("--no-register", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    if args.register_only:
        reg = register_runtime()
        print(json.dumps({"schema": "simplicio.mcp-force-sync/v1", "register": reg}, indent=2))
        return 0 if reg.get("ok") else 1
    if not args.do_global and not args.target:
        args.do_global = True
    try:
        receipt = sync(
            do_global=args.do_global,
            target=Path(args.target) if args.target else None,
            register=not args.no_register,
        )
    except Exception as exc:
        err = {"schema": "simplicio.mcp-force-sync/v1", "ok": False, "error": str(exc)}
        print(json.dumps(err, indent=2) if args.json else f"FAIL: {exc}")
        return 1
    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(f"mcp_force_sync: wrote {receipt['count']} surfaces; register={receipt['register'].get('ok')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
