# Qwen adapter (Qwen Code / Qwen CLI)

Qwen Code (Alibaba's `qwen-code` CLI, a fork of Gemini CLI) and Qwen's other CLI surfaces read a
`.qwen/settings.json`-style config analogous to Gemini CLI's `.gemini/settings.json`, including
`mcpServers` support inherited from the fork. This is documented upstream but **not verified
against a real install in this repo** — treat the config path/shape as best-effort until you
confirm it against your installed Qwen Code version.

## Install

`qwen` is not yet a recognized target of `scripts/install.sh`/`scripts/install_lib.py` (see
`adapters/MATRIX.md` § Install for the currently wired runtime list). Until it is wired in,
install by hand — write a `QWEN.md` (or reuse `AGENTS.md` if your Qwen CLI reads that convention,
as its Gemini CLI upstream does) that loads `.claude/skills/simplicio-tasks/SKILL.md` +
satellites, mirroring the [Gemini adapter](../gemini/README.md)'s approach.

## Loop drive — self-paced

No stop-hook → self-pace via cron / CI tick:

```bash
*/2 * * * *  cd /repo && qwen -p "/simplicio-tasks continue the open queue"
```

## Token economy

`orient_clamp.py` works as-is. Reference it in your Qwen conventions file.

## Native bind — MCP (REQUIRED, best-effort wiring)

`simplicio-runtime` native binding is **REQUIRED** per CLAUDE.md § Hooks — a missing/unreachable
bind BLOCKS the loop preflight on Qwen exactly as on every other adapter.

```bash
pip install -U simplicio-installer && simplicio install --global
```

## MCP config

- **Config file:** `.qwen/settings.json` (project scope) or `~/.qwen/settings.json` (user scope),
  under an `mcpServers` key — inherited from the Gemini CLI fork's schema. **Best-effort**: the
  Qwen Code project documents MCP support but this repo has not mechanically verified the exact
  file path/schema against a live install.
- **Snippet:**

```json
{ "mcpServers": { "simplicio": { "command": "simplicio", "args": ["serve", "--mcp", "--stdio"], "cwd": "/path/to/your/repo" } } }
```

- **Verify:** `simplicio doctor --json | grep -A2 mcp-host-registration` confirms the runtime
  side; use `qwen mcp list` if your CLI version ships that subcommand (mirrors Gemini CLI's
  surface). Tier: **best-effort / community-reported, not gated**.

## Use

```
qwen -p "/simplicio-tasks finish all the open issues"
```

## Progresso do run

Self-paced (N2): the tick echoes the turn-header. Universal fallback (N3): open
`.orchestrator/loop/PROGRESS.md` (auto-regenerated every turn).
