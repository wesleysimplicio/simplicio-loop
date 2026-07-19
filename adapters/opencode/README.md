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

`simplicio-runtime` native binding is **REQUIRED** on OpenCode — a missing/unreachable bind
BLOCKS the loop preflight (CLAUDE.md § Hooks). Add this to `opencode.json`:

```json
{ "mcp": { "simplicio": { "type": "local", "command": ["simplicio", "serve", "--mcp", "--stdio"] } } }
```

Use `simplicio doctor --json` to confirm the bind.

## MCP config

- **Config file:** `opencode.json` (or `opencode.jsonc`) at the repo root, under the **`mcp`**
  key — OpenCode's schema uses `type: "local"` + a `command` array, not `command`/`args` split
  like most other hosts.
- **Snippet:**

```json
{
  "mcp": {
    "simplicio": {
      "type": "local",
      "command": ["simplicio", "serve", "--mcp", "--stdio"],
      "environment": {}
    }
  }
}
```

  (OpenCode inherits the working directory it was launched from; run `opencode` from the target
  repo, or set `environment`/`cwd` per your OpenCode version's config reference.)
- **Verify:** `simplicio doctor --json | grep -A2 mcp-host-registration`, or `opencode mcp list`
  if your version ships that subcommand. Tier: **best-effort** — OpenCode is Tier 2 (provider-
  agnostic MCP support is documented upstream but not mechanically gated here).

## Use

```
opencode run "/simplicio-tasks finish all the open issues"
```

## Progresso do run

Self-paced (N2): the tick echoes the turn-header. Universal fallback (N3, works with any config):
`watch -n5 cat .orchestrator/loop/PROGRESS.md`.
