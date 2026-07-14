# OpenCode adapter

OpenCode is a terminal-native agent that reads `AGENTS.md`, supports MCP servers, and has its
own config (`opencode.json`). No stop-hook → self-paced loop.

## Install

```bash
bash scripts/install.sh opencode
```

The installer ensures `AGENTS.md` loads `.claude/skills/simplicio-tasks/SKILL.md` + satellites
and registers the MCP server in `opencode.json`.

## Loop drive — self-paced

Drive ticks headlessly on a schedule:

```bash
*/2 * * * *  cd /repo && opencode run "/simplicio-tasks continue the open queue"
```

`simplicio-loop` advances the scratchpad and exits on the evidence-gated promise, the cap,
spindle handoff, or explicit STOP.

## Token economy

`orient_clamp.py` works as-is. Reference it in `AGENTS.md` so heavy commands are clamped.

## Native bind — MCP (optional)

`simplicio-runtime` native binding is optional on OpenCode. Add this to `opencode.json` when its
MCP capabilities are useful:

```json
{ "mcp": { "simplicio": { "type": "local", "command": ["simplicio", "serve", "--mcp", "--stdio"] } } }
```

Use `simplicio doctor --json` to diagnose the optional integration.

## Use

```
opencode run "/simplicio-tasks finish all the open issues"
```
