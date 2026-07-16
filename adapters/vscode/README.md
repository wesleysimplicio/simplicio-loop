# VS Code (Copilot) adapter

GitHub Copilot in VS Code reads `.github/copilot-instructions.md` as repo-wide custom
instructions, supports **MCP servers**, and can run **tasks**. We use all three.

## Install

```bash
bash scripts/install.sh vscode
```

The installer writes `.github/copilot-instructions.md` that loads the orchestrator protocol
(it references `.claude/skills/simplicio-tasks/SKILL.md` and the satellites) and registers the
MCP server in `.vscode/mcp.json`.

### Global Windows install (VS Code + GitHub Copilot)

Project-local files are not enough for a user-global Copilot setup. Run the official installer
from this repository with `vscode --global`:

```powershell
pwsh scripts/install.ps1 vscode -Global
```

The global path keeps the project-compatible copy under `~/.claude/skills/`, and also refreshes
the active Copilot surfaces:

- `%USERPROFILE%/.copilot/skills/` — the personal Agent Skills directory;
- `%USERPROFILE%/.copilot/instructions/simplicio-loop.instructions.md` — personal instructions;
- `%USERPROFILE%/.copilot/mcp-config.json` — Copilot CLI user MCP;
- `%APPDATA%/Code/User/mcp.json` — VS Code user MCP.

The installer merges only the `simplicio` server and preserves unrelated skills, instructions,
servers, and settings. It resolves the installed `simplicio` executable (or uses the portable
`simplicio` command name when the runtime is not installed yet). Verify the active surfaces with:

```powershell
python scripts/doctor.py --json
python scripts/doctor.py --repair
```

The VS Code/Copilot global adapter is one surface of the same runtime-neutral loop contract used
by Claude, Codex, Cursor, Gemini, Kiro, Antigravity, Hermes/Simplicio Agent, OpenClaw, and other
providers. GitHub lifecycle comments identify the runtime/device but coordinate all of them through
the same `source_issue`.

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
