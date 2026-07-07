# AGENTS.md — simplicio-loop

This repository ships a runtime-agnostic **super-plugin**: the Universal Looping AI
Orchestrator plus five satellite skills, packaged for 11 runtimes. Any agent runtime that
reads `AGENTS.md` / skill folders can run it.

## What to load

The orchestrator IS the protocol — load it and follow it end-to-end:

```
.claude/skills/simplicio-loop/SKILL.md
```

It is self-contained and uses only standard tools (shell, git, gh, file edit, web), so it
works on any strong LLM. When present, it DELEGATES to its five satellites for deeper,
token-cheaper behavior (it never requires them):

| Skill | Absorbs | Role |
|---|---|---|
| `simplicio-loop` | Ralph Wiggum loop | re-feed the goal until an evidence-gated `<promise>` or a `max_iterations` cap; durable run-journal (attempt memory) + stall detector so it changes strategy instead of oscillating (`scripts/loop_journal.py`); a local **task backlog** (`scripts/task_backlog.py`) freezes a vague goal's multi-item decomposition (per-item ACs, dependency-ordered, genesis-aware) and gates each item's close on the task anchor |
| `simplicio-orient` | rtk + caveman terminal discipline | terminal-first execution, output-reduction catalog, tee-cache, signatures-read |
| `simplicio-review` | thermos | parallel adversarial review on distinct rubrics → deduped verdict |
| `simplicio-compress` | caveman | prose + memory compression, byte-preserving, fail-closed `transform_guard` |
| `simplicio-learn` | continual-learning + teaching | retrospective → durable, deduped lessons in memory |

## Hooks (cross-platform Python, fail-open)

`hooks/` makes the loop + token economy deterministic where the runtime supports hooks:
`loop_stop.py` / `loop_capture.py` (the loop), `orient_clamp.py` (clamp any command's output +
tee-on-failure — works with NO wiring on every runtime), `orient_rewrite.py` (opt-in
auto-clamp). See [`hooks/README.md`](hooks/README.md).

## Runtimes

Install for any of the 11 runtimes with `scripts/install.sh <runtime>` (or `install.ps1`). See
[`adapters/MATRIX.md`](adapters/MATRIX.md): Claude Code · Codex · VS Code (Copilot) · Cursor ·
Antigravity · Kiro · OpenCode · Gemini · Aider · Hermes · OpenClaw.

## Activation

The user invokes it with a target body of work:

```
/simplicio-loop finish all the open issues
/simplicio-loop clear the CI queue
/simplicio-loop drain the Jira board
```

If no argument is given, default to "all open work-items in the default source" and
confirm scope in one line only if ambiguous.

## Extension points (bind native — REQUIRED on 8 of the 11 runtimes)

The skill defines **48 named extension points** (see the Step 1b table in `SKILL.md`).
For each point, if this runtime exposes a faster native capability, **bind it** —
the step becomes deterministic and near-zero-token. The skill never requires a specific
runtime; the binding lives here in the host, not in the skill.

On Claude Code, Codex, Cursor, VS Code, Antigravity, Kiro, OpenCode, and Hermes, binding to
`simplicio-runtime` (MCP or CLI) is **REQUIRED project policy, not an optional speed-up** — see
`adapters/MATRIX.md` `FORCED_BIND_RUNTIMES`. On these 8, an unreachable bind is a STOP-and-report
condition (verify with `simplicio doctor --json`); only Gemini, Aider, and OpenClaw fall back to
the plain LLM/native-by-design path silently.

## Canonical source per topic (#119 — avoid re-stating the same pitch in N docs)

Each topic below has exactly ONE canonical doc; every other file that touches the topic should
link to it instead of repeating the content. When editing one of these topics, edit the canonical
file and let secondary docs (`GEMINI.md`, `PYPI.md`, `llms.txt`, etc.) stay short pointers.

| Topic | Canonical doc | Secondary docs that point here instead of repeating it |
|---|---|---|
| Elevator pitch / what this repo is | `README.md` | `GEMINI.md`, `llms.txt` (one-line summary + link) |
| Runtime-agnostic contract (extension points, non-negotiables) | `AGENTS.md` (this file) | `CLAUDE.md` (Claude-specific overlay only), `GEMINI.md`, `.windsurf/rules/agents.md`, `.kiro/steering/agents.md` (symlinks) |
| Ecosystem / cross-repo dependencies | `SIMPLICIO_ECOSYSTEM.md` | — |
| Pricing / monetization | `PRICING.md` | — |
| Loop mechanics (protocol, exit gates) | `.claude/skills/simplicio-loop/SKILL.md` + `references/*.md` | `AGENTS.md` § Video evidence links in rather than re-describing |
| PyPI package listing copy | `PYPI.md` (wired as `readme` in `pyproject.toml`) | — (this is itself the PyPI-rendered page, so it necessarily carries its own condensed pitch) |
| LLM quick-orientation index | `llms.txt` | — |
| `scripts/*.py` core vs satellite classification | `docs/SCRIPTS_INVENTORY.md` (#118) | `README.md` § Tests & local checks links in rather than re-listing |

## Video evidence (hyperframes)

The orchestrator can **create demo videos** of a screen/feature on request
(`/simplicio-loop make a demo video of screen X`) and reuse them as proof a change works.
The `video_evidence` extension point binds [hyperframes](https://github.com/heygen-com/hyperframes)
(deterministic HTML→MP4; Node 22+ + FFmpeg, no API keys). Worker: `scripts/video_evidence.py`;
contract: `.claude/skills/simplicio-loop/references/video-evidence.md`. It chains after
`web_verify` (screenshots → captioned, deterministic MP4 walkthrough). Evidence is always a file
path + verdict; a missing toolchain BLOCKS, never a fake pass.

## Non-negotiables

- Run commands for real — never simulate output.
- Never mark an item done without green gates + evidence ("works, not just compiles").
- Secret-scan every diff; route irreversible ops through the human gate. Where hooks exist this is
  ENFORCED fail-closed by `hooks/action_gate.py` (PreToolUse/pre-push) — not left to the model.
- Unattended 24/7 runs require persistent source auth, human gate + secret scan, and a reachable
  STOP/cancel path.
- Report token-savings ONLY when a measured receipt backs it (clamp / signatures-read / cache hit /
  `deterministic_edit` / `savings_ledger`); never fabricate a figure. No measured economy → no
  savings line. Credited only on a passing quality gate.
- Verify claims locally before pushing: `python3 scripts/check.py` (test suite + claims-audit +
  `_bundle ≡ source` parity + the token/context budget guard, `scripts/token_budget.py`, #121).
  Self-runs on bare python3 — no CI, no pytest required. Keep it green.
- **Big refactors/doc rewrites:** run `python3 scripts/check.py --token-budget` and treat a FAIL
  as a real regression to justify or trim, not to silence with `--update-baseline` unreviewed.
- On Claude/Codex/Cursor/VS Code/Antigravity/Kiro/OpenCode/Hermes, the `simplicio-runtime` native
  bind (MCP or CLI) is REQUIRED before driving the loop — `simplicio doctor --json` must report
  the runtime reachable. An unbound run on one of these 8 is a blocker to fix
  (`simplicio install --global`), never a silent fallback to the plain-LLM path.
