# Codex adapter

Codex reads `AGENTS.md` as its standing instructions. Point it at the skill; drive the loop
self-paced (Codex has no general stop-hook); bind natively via MCP or the Python adapter.

## Install

```bash
bash scripts/install.sh codex            # writes/links AGENTS.md → SKILL.md, copies skills
```

The installer ensures `AGENTS.md` at the repo root references
`.claude/skills/simplicio-tasks/SKILL.md` (this repo's `AGENTS.md` already does). Codex loads
that on every run.

## Loop drive — self-paced

Codex has no stop-hook, so `simplicio-loop` self-paces: each run does one iteration, checks the
evidence-gated promise, and reschedules itself via the host scheduler until the promise is true,
the cap is hit, spindle handoff is latched, or STOP is signaled. Drive ticks with `codex exec`
on a cron / CI schedule:

```bash
*/2 * * * *  cd /repo && codex exec "/simplicio-tasks continue the open queue"
```

## Token economy

`orient_clamp.py` works as-is. Add it to your `AGENTS.md` command conventions so Codex routes
heavy commands through it:

```
python3 hooks/orient_clamp.py -- <build/test/diff command>
```

## Native bind (REQUIRED)

`simplicio-runtime` native binding is **REQUIRED** on Codex — a missing/unreachable bind BLOCKS
the loop preflight (CLAUDE.md § Hooks), even though drive itself stays self-paced:

```bash
pip install -U simplicio-installer && simplicio install --global   # registers Codex's MCP client
# or use the Python adapter at simplicio-runtime/agent/codex_responses_adapter.py
```

Use `simplicio doctor --json` to confirm the bind before scheduling ticks.

## MCP config

- **Config file:** `~/.codex/config.toml`, under an `[mcp_servers.<name>]` table.
- **Snippet:**

```toml
[mcp_servers.simplicio]
command = "simplicio"
args = ["serve", "--mcp", "--stdio"]
cwd = "/path/to/your/repo"
```

- **Verify:** `simplicio doctor --json | grep -A2 mcp-host-registration`, or `codex mcp list` if
  your Codex CLI version ships that subcommand. Tier: **verified** — Codex is a gated Tier 1
  runtime (`scripts/verify_adapters.py codex`).

## Use

```
codex exec "/simplicio-tasks finish all the open issues"
```

## Progresso do run

Self-paced (N2): the tick echoes `python3 scripts/loop_progress.py render --turn-header` at the
start of every turn — the % is right there in the transcript. For a live panel outside the
transcript (N3, universal): `watch -n5 cat .orchestrator/loop/PROGRESS.md`.
