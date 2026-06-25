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
| `simplicio-loop` | Ralph Wiggum loop | re-feed the goal until an evidence-gated `<promise>` or a `max_iterations` cap; durable run-journal (attempt memory) + stall detector so it changes strategy instead of oscillating (`scripts/loop_journal.py`) |
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

The skill defines **48 named extension points** (see the Step 1b table in `SKILL.md`).
For each point, if this runtime exposes a faster native capability, **bind it** —
the step becomes deterministic and near-zero-token. If not, perform the documented
LLM fallback. The skill never requires a specific runtime; the binding lives here in
the host, not in the skill.

## Video evidence (hyperframes)

The orchestrator can **create demo videos** of a screen/feature on request
(`/simplicio-tasks make a demo video of screen X`) and reuse them as proof a change works.
The `video_evidence` extension point binds [hyperframes](https://github.com/heygen-com/hyperframes)
(deterministic HTML→MP4; Node 22+ + FFmpeg, no API keys). Worker: `scripts/video_evidence.py`;
contract: `.claude/skills/simplicio-tasks/references/video-evidence.md`. It chains after
`web_verify` (screenshots → captioned, deterministic MP4 walkthrough). Evidence is always a file
path + verdict; a missing toolchain BLOCKS, never a fake pass.

## Non-negotiables

- Run commands for real — never simulate output.
- Never mark an item done without green gates + evidence ("works, not just compiles").
- Secret-scan every diff; route irreversible ops through the human gate. Where hooks exist this is
  ENFORCED fail-closed by `hooks/action_gate.py` (PreToolUse/pre-push) — not left to the model.
- Unattended 24/7 runs require a cost kill-switch (`ceiling > 0`).
- Report token-savings ONLY when a measured receipt backs it (clamp / signatures-read / cache hit /
  `deterministic_edit` / `savings_ledger`); never fabricate a figure. No measured economy → no
  savings line. Credited only on a passing quality gate.
- Verify claims locally before pushing: `python3 scripts/check.py` (test suite + claims-audit +
  `_bundle ≡ source` parity). Self-runs on bare python3 — no CI, no pytest required. Keep it green.
