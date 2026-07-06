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

## Native bind — MCP (REQUIRED)

`simplicio-runtime` native bind is REQUIRED on OpenCode, not optional. `scripts/install.sh
opencode` writes this to `opencode.json` for you (`merge_opencode_mcp` in
`scripts/install_lib.py`):

```json
{ "mcp": { "simplicio": { "type": "local", "command": ["simplicio", "serve", "--mcp", "--stdio"] } } }
```

Verify with `simplicio doctor --json` before relying on the orchestrator.

## Use

```
opencode run "/simplicio-tasks finish all the open issues"
```
