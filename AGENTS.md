# AGENTS.md — simplicio-tasks

This repository ships a runtime-agnostic **super-plugin**: the Universal Looping AI
Orchestrator plus five satellite skills, packaged for 11 runtimes. Any agent runtime that
reads `AGENTS.md` / skill folders can run it.

## What to load

The orchestrator IS the protocol — load it and follow it end-to-end:

```
.claude/skills/simplicio-tasks/SKILL.md
```

It is self-contained and uses only standard tools (shell, git, gh, file edit, web), so it
works on any strong LLM. When present, it DELEGATES to its five satellites for deeper,
token-cheaper behavior (it never requires them):

| Skill | Absorbs | Role |
|---|---|---|
| `simplicio-loop` | Ralph Wiggum loop | re-feed the goal until an evidence-gated `<promise>` or a `max_iterations` cap |
| `simplicio-orient` | rtk + caveman terminal discipline | terminal-first execution, output-reduction catalog, tee-cache, signatures-read |
| `simplicio-review` | thermos | parallel adversarial review on distinct rubrics → deduped verdict |
| `simplicio-compress` | caveman | prose + memory compression, byte-preserving, fail-closed `transform_guard` |
| `simplicio-learn` | continual-learning + teaching | retrospective → durable, deduped lessons in memory |

## Hooks (cross-platform Python, fail-open)

`hooks/` makes the loop + token economy deterministic where the runtime supports hooks:
`loop_stop.py` / `loop_capture.py` (the loop), `orient_clamp.py` (clamp any command's output +
tee-on-failure — works with NO wiring on every runtime), `orient_rewrite.py` (opt-in
auto-clamp), `learn_stop.py` (queue a retrospective). See [`hooks/README.md`](hooks/README.md).

## Runtimes

Install for any of the 11 runtimes with `scripts/install.sh <runtime>` (or `install.ps1`). See
[`adapters/MATRIX.md`](adapters/MATRIX.md): Claude Code · Codex · VS Code (Copilot) · Cursor ·
Antigravity · Kiro · OpenCode · Gemini · Aider · Hermes · OpenClaw.

## Activation

The user invokes it with a target body of work:

```
/simplicio-tasks finish all the open issues
/simplicio-tasks clear the CI queue
/simplicio-tasks drain the Jira board
```

If no argument is given, default to "all open work-items in the default source" and
confirm scope in one line only if ambiguous.

## Extension points (bind native, else fall back)

The skill defines **43 named extension points** (see the Step 1b table in `SKILL.md`).
For each point, if this runtime exposes a faster native capability, **bind it** —
the step becomes deterministic and near-zero-token. If not, perform the documented
LLM fallback. The skill never requires a specific runtime; the binding lives here in
the host, not in the skill.

## Non-negotiables

- Run commands for real — never simulate output.
- Never mark an item done without green gates + evidence ("works, not just compiles").
- Secret-scan every diff; route irreversible ops through the human gate.
- Unattended 24/7 runs require a cost kill-switch (`ceiling > 0`).
- Report an honest token-savings line, credited only on a passing quality gate.
