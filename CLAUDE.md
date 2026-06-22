# CLAUDE.md — simplicio-tasks (Claude Code)

This repo ships **simplicio-tasks**, a runtime-agnostic **super-plugin**: an autonomous
looping orchestrator plus five satellite skills, packaged for 11 runtimes.

## The 6 skills

| Skill | Role |
|---|---|
| `simplicio-tasks` | the orchestrator loop (discover → implement → verify → merge → close → watch 24/7) |
| `simplicio-loop` | hardened Ralph loop — re-feed the goal until an evidence-gated `<promise>` or a cap |
| `simplicio-orient` | terminal-first token economy — output-reduction catalog, tee-cache, signatures-read |
| `simplicio-review` | thermos-style parallel adversarial review on distinct rubrics → deduped verdict |
| `simplicio-compress` | caveman-style prose + memory compression, byte-preserving, `transform_guard` |
| `simplicio-learn` | retrospective → durable, deduped lessons written to memory |

They live in `.claude/skills/` and load automatically in this repo.

## Install (this or another project)

```bash
# project-local (copies skills, wires Stop + PreToolUse hooks)
bash scripts/install.sh claude
# global (all projects)
bash scripts/install.sh claude --global
# Windows
pwsh scripts/install.ps1 claude
```

Or as a marketplace plugin:

```
/plugin marketplace add wesleysimplicio/simplicio-tasks
/plugin install simplicio-tasks@simplicio
```

## Use

```
/simplicio-tasks finish all the open issues
```

## Hooks (the loop + token economy)

`hooks/` ships cross-platform Python hooks (fail-open): `loop_stop.py` (re-feed/exit),
`loop_capture.py` (promise detect), `orient_clamp.py` (clamp any command's output, tee on
failure), `orient_rewrite.py` (opt-in auto-clamp), `learn_stop.py` (queue retrospective). See
[`hooks/README.md`](hooks/README.md) for Claude `settings.json` wiring (the installer does it).

`orient_clamp.py` needs no wiring — `python3 hooks/orient_clamp.py -- <cmd>` anywhere.

Claude's native tools satisfy the extension points: sub-agents → `execute`, file tools →
`deterministic_edit`, the scheduler → `watcher`. Where `simplicio-runtime` is installed,
`simplicio mcp register --client claude-code` binds them deterministically.

## Other runtimes

The same skills run on Codex, VS Code (Copilot), Cursor, Antigravity, Kiro, OpenCode, Gemini,
Aider, Hermes, and OpenClaw — see [`adapters/MATRIX.md`](adapters/MATRIX.md) and
[`AGENTS.md`](AGENTS.md) for the runtime-agnostic contract (43 extension points; the binding
lives in the host, never in the skill).
