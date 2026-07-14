# Gemini adapter

Gemini CLI reads `GEMINI.md` as its standing context and supports MCP servers. Point it at the
skill; drive the loop self-paced; bind natively via MCP or the simplicio-runtime Gemini adapter.

## Install

```bash
bash scripts/install.sh gemini
```

This repo's `GEMINI.md` already loads `.claude/skills/simplicio-tasks/SKILL.md`; the installer
adds the satellites and registers the MCP server in `.gemini/settings.json`.

## Loop drive — self-paced

No stop-hook → self-pace via cron / CI tick:

```bash
*/2 * * * *  cd /repo && gemini -p "/simplicio-tasks continue the open queue"
```

## Token economy

`orient_clamp.py` works as-is. Add it to `GEMINI.md` command conventions.

## Native bind — MCP / native adapter

```bash
simplicio-cli mcp register --client gemini
# or use simplicio-runtime/agent/gemini_native_adapter.py for the native REST path
```

`.gemini/settings.json`:

```json
{ "mcpServers": { "simplicio": { "command": "simplicio", "args": ["mcp", "serve"] } } }
```

## Use

```
gemini -p "/simplicio-tasks finish all the open issues"
```

## Progresso do run

Self-paced (N2): the tick echoes the turn-header (`render --turn-header`). Universal fallback
(N3): open `.orchestrator/loop/PROGRESS.md` (auto-regenerated every turn).
