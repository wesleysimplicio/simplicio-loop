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

## Native bind — MCP / native adapter (REQUIRED)

`simplicio-runtime` native binding is **REQUIRED** on Gemini — a missing/unreachable bind BLOCKS
the loop preflight (CLAUDE.md § Hooks).

```bash
pip install -U simplicio-installer && simplicio install --global
# or use simplicio-runtime/agent/gemini_native_adapter.py for the native REST path
```

## MCP config

Two related surfaces share the Gemini name; treat them separately:

- **Gemini CLI** — **Config file:** `~/.gemini/settings.json` (user scope) or `.gemini/settings.json`
  (project scope), under an `mcpServers` key. **Verified** conceptually (documented Gemini CLI MCP
  format); this repo's installer writes this file.

```json
{ "mcpServers": { "simplicio": { "command": "simplicio", "args": ["serve", "--mcp", "--stdio"], "cwd": "/path/to/your/repo" } } }
```

- **Gemini Code Assist** (the IDE/VS Code extension side, distinct from the CLI) — uses its own
  IDE-level MCP settings surface, not the `.gemini/settings.json` file above. **Not verified**
  against a real Code Assist install in this repo — best-effort; check the extension's own MCP
  settings UI for the current field names before relying on a JSON snippet here.

- **Verify:** `simplicio doctor --json | grep -A2 mcp-host-registration`, or `gemini mcp list` if
  your Gemini CLI version ships that subcommand. Tier: **best-effort** — Gemini is Tier 2 overall;
  the CLI path is the more reliable of the two surfaces.

## Use

```
gemini -p "/simplicio-tasks finish all the open issues"
```

## Progresso do run

Self-paced (N2): the tick echoes the turn-header (`render --turn-header`). Universal fallback
(N3): open `.orchestrator/loop/PROGRESS.md` (auto-regenerated every turn).
