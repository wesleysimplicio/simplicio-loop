# CLAUDE.md — simplicio-loop (Claude Code)

This repo ships **simplicio-loop**, a runtime-agnostic **super-plugin**: an autonomous
looping orchestrator (the `/simplicio-loop` skill) plus six satellite skills, packaged for 12
runtimes.

## The 7 skills

| Skill | Role |
|---|---|
| `simplicio-loop` | unified public entrypoint: orchestrator core + hardened Ralph loop — re-feed the goal until an evidence-gated `<promise>` or a cap; durable run-journal (attempt memory) + stall detector (`scripts/loop_journal.py`) so it switches strategy instead of oscillating, plus a **task anchor** (`scripts/task_anchor.py`) — durable memory for SCOPE that freezes the acceptance criteria and blocks drift / "done" while any AC is unverified — and, above it, a **task backlog** (`scripts/task_backlog.py`, SKILL.md § Phase 0): the frozen multi-item LLM decomposition (per-item ACs + `depends_on`), genesis-aware (an empty repo leads with a `scaffold` item), whose `done` is gated on the verified anchor |
| `simplicio-tasks` | legacy alias kept only for compatibility with older installs and saved prompts |
| `simplicio-orient` | terminal-first token economy — output-reduction catalog, tee-cache, signatures-read |
| `simplicio-review` | thermos-style parallel adversarial review on distinct rubrics → deduped verdict |
| `simplicio-compress` | caveman-style prose + memory compression, byte-preserving, `transform_guard` |
| `simplicio-learn` | retrospective → durable, deduped lessons written to memory |
| `simplicio-autoresearch` | evolutionary mutate/eval/keep-revert optimizer (Karpathy `autoresearch`) — yool-guardrailed caps, git-isolated branch, anti-Goodhart gate-first eval, `savings-event` receipt (`scripts/autoresearch.py`) |

They live in `.claude/skills/` and load automatically in this repo.

## The 2 bound operators (REQUIRED by the loop)

`simplicio-loop` does not survey or edit with the LLM — it delegates to two installed CLIs. The
supported install surface is the single package `simplicio-cli`, which exposes
`simplicio-dev-cli` and also brings `simplicio-mapper` transitively; the loop BLOCKS if either
runtime binary is absent:

| Operator | Binary | pip pkg | Binds | Role |
|---|---|---|---|---|
| [simplicio-mapper](https://github.com/wesleysimplicio/simplicio-mapper) | `simplicio-mapper` | transitively via `simplicio-cli` | `orient` | **survey** the repo → `.simplicio/*.json` (the survey that feeds the goal) |
| [simplicio-dev-cli](https://github.com/wesleysimplicio/simplicio-dev-cli) | `simplicio-dev-cli` | `simplicio-cli` | `execute`/`deterministic_edit` | **operate** — apply+verify each decided change via its 6-layer contract, instead of the AI hand-editing |

The AI decides; the operators act. See `.claude/skills/simplicio-loop/SKILL.md` § Bound operators
and `.claude/skills/simplicio-loop/references/extension-points.md` § bound operators.

## Video evidence (Playwright by default · hyperframes on request)

The loop produces **demo videos** as proof a change works — two engines, one `video_evidence`
extension point. The **normal evidence flow uses Playwright**: `video_evidence verify --url …`
records the **real browser session** driving the screen (`.webm`, → `.mp4` with FFmpeg) — the
"works, not just compiles" moving proof for any UI change. **hyperframes** is used **only for an
explicit custom request** — *"make an explainer video of screen X"* — rendering a deterministic,
captioned slideshow of the `web_verify` screenshots
([hyperframes](https://github.com/heygen-com/hyperframes), Node 22+ + FFmpeg, no API keys). Worker:
`scripts/video_evidence.py`; contract:
`.claude/skills/simplicio-loop/references/video-evidence.md`. A missing toolchain BLOCKS, never a
fake pass.

## PR evidence (prints + item-by-item AC check on every PR)

The PR body is **assembled mechanically**, never hand-written, so it always shows the proof. Worker
`scripts/pr_evidence.py build --require-evidence` pulls the **item-by-item acceptance-criteria
checklist** from the task anchor (`scripts/task_anchor.py`, frozen at intake) AND embeds the
screenshots (`web_verify`, under `.orchestrator/tee/web`) and recordings (`video_evidence`, under
`.orchestrator/tee/video`). With
`--require-evidence` it FAILS CLOSED (exit 3, `blocked`) rather than open a PR that has neither a
checklist nor a print — the executable answer to "the PR opened without prints / without an
item-by-item check of the task". It honors a discovered `.github/PULL_REQUEST_TEMPLATE.md` (keeps the
maintainer's sections, appends checklist + prints below). The **task anchor** is the same worker that
stops task deviation: every turn re-checks the frozen goal (`task_anchor.py check`) and the DoD gate
(`task_anchor.py gate`) blocks "done" while any AC is unverified.

## Progress feedback (real-time, EPIC #296)

`scripts/loop_progress.py` computes "onde estamos / quanto falta" deterministically from the
backlog + anchor + its own event trail — never fabricated. Three surfaces, one denominator:
**N1 hook** (Claude/Cursor re-feed header shows fase/etapa/item/ACs/%), **N2 transcript**
(every turn's first line is `render --turn-header`, normative on all 12 runtimes), **N3 file**
(`.orchestrator/loop/PROGRESS.md`/`progress.json`, regenerated every turn — the universal
fallback any host, adapted or not, can read with zero extra code). Status command:
`python3 scripts/loop_progress.py status --json`. Full contract, event schema, and the
turn×event/runtime×level tables: `.claude/skills/simplicio-loop/references/progress-feedback.md`.

## Tests & local checks (no paid CI)

`python3 scripts/check.py` runs the whole gate locally: the `tests/` suite (worker `selftest`s + an
e2e of the loop driver proving it stops on EVIDENCE, ignores a bare `<promise>`, stops on the cap;
+ producers BLOCK, never fake-pass, when a toolchain is absent), `scripts/claims_audit.py`
(referenced scripts exist · extension-point count consistent · cited commands run · `_bundle ≡
source`), and the **token/context budget guard** (`scripts/token_budget.py`, #121) — estimates
tokens for SKILL.md/AGENTS.md/CLAUDE.md/the largest scripts and FAILS on a regression past the
committed baseline (`scripts/token_budget_baseline.json`), so a doc/script that quietly balloons
in size on a big refactor is caught the same way a broken test would (`--token-budget` runs it
alone; `--update-baseline` regenerates the baseline after a deliberate, reviewed size change).
Self-runs on bare python3 (no pytest needed, no `tiktoken` needed — a stdlib chars/4 heuristic is
the default estimator); `pip install "simplicio-loop[dev]"` adds pytest. Wire as a git pre-push
hook to keep work honest with zero CI cost.

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
/plugin marketplace add wesleysimplicio/simplicio-loop
/plugin install simplicio-loop@simplicio
```

The marketplace install carries only the **lean `plugin/` subdirectory** (the 7 skills + the 5
wired hooks) — `.claude-plugin/marketplace.json` `source` points at `./plugin`, so the pip-only
assets (capture proxy `engine/`, token-monitor dashboard) are NOT copied into a user's
plugin cache. `plugin/` is generated from source by `python3 scripts/sync_plugin.py` (run it after
editing skills or a wired hook); `scripts/check.py` fails if `plugin/` drifts from source.

## Use

```
/simplicio-loop finish all the open issues
```

## Hooks (the loop + token economy)

`hooks/` ships cross-platform Python hooks (fail-open): `loop_stop.py` (re-feed/exit),
`loop_capture.py` (promise detect), `orient_clamp.py` (clamp any command's output, tee on
failure), `orient_rewrite.py` (opt-in auto-clamp). See [`hooks/README.md`](hooks/README.md) for
Claude `settings.json` wiring (the installer does it).

`orient_clamp.py` needs no wiring — `python3 hooks/orient_clamp.py -- <cmd>` anywhere.

**Safety is enforced, not just described:** `hooks/action_gate.py` is a **fail-closed**
`PreToolUse` (Bash) / git-pre-push hook that BLOCKS irreversible ops (force-push, history rewrite,
mass-delete, destructive DDL, infra teardown) and secret-laden commits/pushes before they run
(exit 2) — Step 5 made mechanical. `python3 hooks/action_gate.py selftest` proves the ruleset.

Claude's native tools satisfy the extension points: sub-agents → `execute`, file tools →
`deterministic_edit`, the scheduler → `watcher`. A `simplicio-runtime` native bind is optional on
Claude Code and every other adapter; install it when its MCP/CLI capabilities are useful, but do
not block an unbound loop.

## Other runtimes

The same skills run on Codex, VS Code (Copilot), Cursor, Antigravity, Kiro, OpenCode, Gemini,
Aider, Simplicio Agent (formerly Hermes), OpenClaw, and Orca ([onorca.dev](https://www.onorca.dev/docs),
the worktree IDE — see `adapters/orca/README.md`) — see [`adapters/MATRIX.md`](adapters/MATRIX.md) and
[`AGENTS.md`](AGENTS.md) for the runtime-agnostic contract (49 extension points; the binding
lives in the host, never in the skill).
