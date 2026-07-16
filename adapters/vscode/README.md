# VS Code (Copilot) adapter

GitHub Copilot in VS Code reads `.github/copilot-instructions.md` as repo-wide custom
instructions, supports **MCP servers**, and can run **tasks**. We use all three.

## Install

Two distinct modes:

### Project-local (this repo only)

```bash
bash scripts/install.sh vscode
```

Writes `.github/copilot-instructions.md` (loads the orchestrator protocol) and registers the
MCP server in `.vscode/mcp.json` — scoped to the current repo.

### User-global (all repos, wires the real runtime surfaces)

```bash
python3 scripts/install_lib.py vscode --global
```

A global install no longer only drops skills under `~/.claude/skills`. It also mirrors the
skills into the **user-level VS Code/Copilot skill surface** and registers the **user-level
MCP server** (`mcp.json` under the OS user config dir), so GitHub Copilot + VS Code are usable
end-to-end on Windows/macOS/Linux without manual copy or MCP path repair. The user-level paths
resolved per OS:

| OS | User-level skill root | User-level MCP |
|----|----------------------|----------------|
| Windows | `%USERPROFILE%\.vscode\simplicio-skills` | `%APPDATA%\Code\User\mcp.json` |
| macOS | `~/Library/Application Support/Code/User` (mcp) · `~/.vscode/simplicio-skills` | `~/Library/Application Support/Code/User/mcp.json` |
| Linux | `~/.config/Code/User` (mcp) · `~/.vscode/simplicio-skills` | `~/.config/Code/User/mcp.json` |

Verify the install actually wired the active surfaces:

```bash
python3 scripts/doctor.py --json   # reports vscode_global drift (OPTIONAL tier)
python3 scripts/doctor.py --repair  # re-syncs skills + MCP if drift detected
```

Idempotent: a second `--global` run is a clean refresh, never a nested copy.

## Loop drive — self-paced via tasks

Copilot has no stop-hook. Drive the loop with a VS Code task that re-invokes the agent, or run
`simplicio-loop` self-paced. Minimal `.vscode/tasks.json` tick:

```jsonc
{ "version": "2.0.0", "tasks": [
  { "label": "simplicio-loop tick", "type": "shell",
    "command": "python3 hooks/loop_stop.py < NUL" } ]
}
```

(The agent itself does the work each turn; the task only advances the scratchpad when running
headless. In interactive chat, just keep saying "continue" — the protocol is idempotent.)

## Token economy

`orient_clamp.py` works as-is in the integrated terminal. Reference it in
`copilot-instructions.md` so Copilot routes heavy commands through it.

## Native bind — MCP (optional)

`simplicio-runtime` native binding is optional on VS Code/Copilot. `simplicio install --global`
can write `.vscode/mcp.json` when you choose to enable it:

```json
{ "servers": { "simplicio": { "command": "simplicio", "args": ["serve", "--mcp", "--stdio"] } } }
```

Then the extension points bind to `simplicio-runtime` natively. Use `simplicio doctor --json` to
diagnose that optional integration.

## Use

Open Copilot Chat and type: `/simplicio-tasks finish all the open issues` (or paste the goal —
the instructions file makes Copilot follow the protocol).

## Progresso do run

Self-paced (N2, via tasks): the task tick echoes the turn-header. Simplest (N3, universal): open
`.orchestrator/loop/PROGRESS.md` in the editor — it auto-updates every turn, no extension needed.
