# Kiro adapter

Kiro (AWS's agentic IDE) uses **steering files** (`.kiro/steering/*.md`) for standing guidance,
**specs** for structured work, and MCP servers. We load the protocol via steering and drive the
loop through specs / self-pacing.

## Install

```bash
bash scripts/install.sh kiro
```

The installer writes `.kiro/steering/simplicio-tasks.md` that loads the orchestrator + satellites
and registers the MCP server in `.kiro/settings/mcp.json`.

## Loop drive — self-paced via specs

No stop-hook. Use a Kiro **spec** as the durable goal, and let `simplicio-loop` self-pace each
execution against the spec's acceptance criteria (which map directly onto the skill's AC gate).
Exit conditions unchanged (evidence-gated promise, cap, budget).

## Token economy

`orient_clamp.py` works as-is. Add it to the steering file's command conventions.

## Native bind — MCP

`.kiro/settings/mcp.json`:

```json
{ "mcpServers": { "simplicio": { "command": "simplicio", "args": ["mcp", "serve"] } } }
```

## Use

Create a spec or chat: `/simplicio-tasks finish all the open issues`. The steering file makes
Kiro follow the protocol and honor the safety gates.
