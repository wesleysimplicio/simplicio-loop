# Orca adapter

[Orca](https://www.onorca.dev/docs) is a desktop **worktree IDE for AI coding agents**: it runs
multiple agent CLIs (Claude Code, Codex, Cursor CLI) in parallel, each task in its own isolated
**git worktree** with its own terminal and browser tab, plus agent hooks & memory, worktree
checkpoints, scheduled automations, and an MCP/skills registry.

Orca is a **host of hosts**: the simplicio-loop protocol runs inside whichever inner agent CLI
Orca drives. The adapter therefore installs into the *repo* (which every Orca worktree sees), and
the inner runtime picks the skills up natively.

## Install

```bash
bash scripts/install.sh orca            # macOS/Linux
pwsh scripts/install.ps1 orca           # Windows
```

The installer copies the 6 skills into `.claude/skills/` and writes the idempotent
`simplicio-loop` marker block into `AGENTS.md` — the two discovery surfaces the inner agents use
(Claude Code/Cursor read the skills tree; Codex reads `AGENTS.md`). Both are committed repo
files, so **every Orca worktree inherits them automatically** — no per-task setup.

## Skill load

Via the inner agent: `.claude/skills/` (Claude Code, Cursor CLI) or `AGENTS.md → SKILL.md`
(Codex). Orca's own skills registry can additionally surface `/simplicio-loop` as a first-class
command; that registration is optional and never replaces the repo-level install.

## Loop drive — inner hook where available, else scheduled automations

- **Inner agent = Claude Code or Cursor CLI** → the real stop-hook drive applies unchanged
  (`hooks/loop_stop.py` / `loop_capture.py`), exactly as in the [claude](../claude/README.md) /
  [cursor](../cursor/README.md) adapters.
- **Inner agent = Codex (or anything hook-less)** → self-paced drive via **Orca scheduled
  automations**: schedule a tick that re-invokes `/simplicio-loop` per the skill's "Self-paced
  drive" section. Same exit conditions (evidence-gated promise, `max_iterations` cap, STOP).

**Worktree isolation fits the loop's state model.** All loop state (`.orchestrator/loop/`,
`.orchestrator/backlog/`) is per-worktree, so each Orca task runs its own independent loop — one
scratchpad, one journal, one anchor per task, with no cross-task interference. Orca's worktree
checkpoints compose with (never replace) the loop's own journal + evidence gates.

## Native bind — MCP (optional)

Tier 2, best-effort: the native `simplicio-runtime` bind via Orca's MCP registry is **optional**
(like Gemini/Aider/OpenClaw — Orca is not in `FORCED_BIND_RUNTIMES`). When the inner agent is one
of the 8 forced-bind runtimes (e.g. Claude Code), that runtime's own REQUIRED-bind policy still
applies inside the Orca session; verify with `simplicio doctor --json`.

## Token economy

`orient_clamp.py` works as-is in every Orca terminal (`python3 hooks/orient_clamp.py -- <cmd>`),
no wiring.

## Use

Open a task in Orca (it allocates the worktree), then in the task's agent session:

```
/simplicio-loop finish all the open issues
```

Manual smoke (the one step a file-level harness can't do): run a small `/simplicio-loop` task in
an Orca worktree, confirm the loop drives (hook or scheduled tick), the gates fire, and the state
stays inside that worktree's `.orchestrator/`.
