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

The installer copies the 7 skills into `.claude/skills/` and writes the idempotent
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

Tier 2, best-effort: the native `simplicio-runtime` bind via Orca's MCP registry is an
**optional acceleration**, never a precondition — an absent or unreachable bind falls back to
the standard-tool protocol with the same evidence and safety gates. `simplicio doctor --json`
can diagnose an installed runtime inside the Orca session.

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

## Progresso do run

Inner-agent hook or self-paced tick (N1/N2 depending on how the inner agent is configured): the
turn-header contract applies identically inside the Orca worktree. Universal fallback (N3): open
`.orchestrator/loop/PROGRESS.md` inside that worktree (auto-regenerated every turn, scoped to the
worktree like all other loop state).

## Status e comentários automáticos

Quando o run está dentro de um worktree Orca ativo, o loop consulta `orca worktree current`
e atualiza somente o card desse worktree com `orca worktree set`: o status usa `todo`,
`in-progress`, `in-review` ou `completed`, e o comentário contém o estado lifecycle, run e
uma mensagem curta. Fora do Orca, a sincronização é um no-op tipado e não toca outro card.

No GitHub, o workflow de lifecycle mantém o comentário canônico, a label e o `Status` do
Project pertencente ao próprio repositório; se a issue ainda não estiver no Project, ela é
adicionada antes da movimentação. `SIMPLICIO_PROJECT_NUMBER` continua sendo aceito para
selecionar explicitamente um Project quando houver mais de um.
