# Antigravity adapter

Antigravity (Google's agentic IDE) is a strong agent runtime with MCP support and a
rules/instructions file. It has no public stop-hook, so the loop self-paces.

## Install

```bash
bash scripts/install.sh antigravity
```

The installer writes an `AGENTS.md` / rules entry that loads
`.claude/skills/simplicio-tasks/SKILL.md` + the satellites, and registers the MCP server.

## Loop drive — self-paced

No stop-hook → `simplicio-loop` self-paces via the IDE's task runner or an OS cron tick that
re-invokes the agent. Same exit conditions (evidence-gated promise, cap, STOP). In
interactive use, keep the agent going with "continue" — the protocol is idempotent and
resumes from the journal.

## Token economy

`orient_clamp.py` works as-is in the terminal. Reference it in the rules file so the agent
routes heavy build/test/diff commands through it.

## Native bind — MCP (optional)

`simplicio-runtime` native binding is optional on Antigravity. A missing/unreachable bind reports
explicit degraded mode while the standalone loop remains available.

```bash
pip install -U simplicio-installer && simplicio install --global
```

Use `simplicio doctor --json` to confirm the bind. Antigravity's exact MCP config path isn't
auto-written by the installer yet, so finish it by hand from the snippet below.

## MCP config

- **Config file:** Antigravity (Google's agentic IDE, a VS Code fork) reads MCP servers from its
  own settings surface, documented by Google as following the same shape as the Gemini Code
  Assist / VS Code MCP settings — typically an IDE settings JSON with an `mcpServers` block, or
  the workspace `.vscode/mcp.json`-style file if the fork keeps that convention. **Not verified
  against a real Antigravity install in this repo** — confirm the exact path in your IDE's MCP
  settings UI before relying on the snippet below.
- **Snippet (best-effort, mirrors the VS Code/Gemini shape):**

```json
{ "mcpServers": { "simplicio": { "command": "simplicio", "args": ["serve", "--mcp", "--stdio"], "cwd": "/path/to/your/repo" } } }
```

- **Verify:** `simplicio doctor --json | grep -A2 mcp-host-registration` confirms the `simplicio`
  binary/runtime side; confirm the IDE side from Antigravity's own MCP status panel if it has
  one. Tier: **best-effort / not mechanically gated** — Antigravity is Tier 2.

## Use

Point the agent at: `/simplicio-tasks finish all the open issues` (or paste the goal — the
rules file makes it follow the protocol).

## Progresso do run

Self-paced (N2): the tick echoes `python3 scripts/loop_progress.py render --turn-header`.
Universal fallback (N3): open `.orchestrator/loop/PROGRESS.md` (auto-regenerated every turn).
