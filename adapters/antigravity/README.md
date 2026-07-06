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

## Native bind — MCP (REQUIRED)

`simplicio-runtime` native bind is REQUIRED on Antigravity, not optional.

```bash
pip install -U simplicio-installer && simplicio install --global
# or add to the IDE's MCP config:  { "simplicio": { "command": "simplicio", "args": ["serve","--mcp","--stdio"] } }
```

Verify with `simplicio doctor --json`; `scripts/install.sh antigravity` checks this for you and
warns loudly if the bind can't be confirmed (Antigravity's exact MCP config path isn't
auto-written by the installer yet — finish it by hand from the snippet above if so).

## Use

Point the agent at: `/simplicio-tasks finish all the open issues` (or paste the goal — the
rules file makes it follow the protocol).
