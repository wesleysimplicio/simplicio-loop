# CLAUDE.md — simplicio-loop (Claude Code)

This repo ships **simplicio-loop**, a runtime-agnostic **super-plugin**: an autonomous
looping orchestrator (the `/simplicio-tasks` skill) plus five satellite skills, packaged for 11
runtimes.

## The 6 skills

| Skill | Role |
|---|---|
| `simplicio-tasks` | the orchestrator loop (discover → implement → verify → merge → close → watch 24/7) |
| `simplicio-loop` | hardened Ralph loop — re-feed the goal until an evidence-gated `<promise>` or a cap; durable run-journal (attempt memory) + stall detector (`scripts/loop_journal.py`) so it switches strategy instead of oscillating |
| `simplicio-orient` | terminal-first token economy — output-reduction catalog, tee-cache, signatures-read |
| `simplicio-review` | thermos-style parallel adversarial review on distinct rubrics → deduped verdict |
| `simplicio-compress` | caveman-style prose + memory compression, byte-preserving, `transform_guard` |
| `simplicio-learn` | retrospective → durable, deduped lessons written to memory |

They live in `.claude/skills/` and load automatically in this repo.

## The 2 bound operators (REQUIRED by the loop)

`simplicio-loop` does not survey or edit with the LLM — it delegates to two installed CLIs, hard
deps of `pip install simplicio-loop` (the loop BLOCKS if either is absent):

| Operator | Binary | pip pkg | Binds | Role |
|---|---|---|---|---|
| [simplicio-mapper](https://github.com/wesleysimplicio/simplicio-mapper) | `simplicio-mapper` | `simplicio-mapper` | `orient` | **survey** the repo → `.simplicio/*.json` (the survey that feeds the goal) |
| [simplicio-dev-cli](https://github.com/wesleysimplicio/simplicio-dev-cli) | `simplicio-dev-cli` | `simplicio-cli` | `execute`/`deterministic_edit` | **operate** — apply+verify each decided change via its 6-layer contract, instead of the AI hand-editing |

The AI decides; the operators act. See `.claude/skills/simplicio-loop/SKILL.md` § Bound operators
and `.claude/skills/simplicio-tasks/references/extension-points.md` § bound operators.

## Video evidence (Playwright by default · hyperframes on request)

The loop produces **demo videos** as proof a change works — two engines, one `video_evidence`
extension point. The **normal evidence flow uses Playwright**: `video_evidence verify --url …`
records the **real browser session** driving the screen (`.webm`, → `.mp4` with FFmpeg) — the
"works, not just compiles" moving proof for any UI change. **hyperframes** is used **only for an
explicit custom request** — *"make an explainer video of screen X"* — rendering a deterministic,
captioned slideshow of the `web_verify` screenshots
([hyperframes](https://github.com/heygen-com/hyperframes), Node 22+ + FFmpeg, no API keys). Worker:
`scripts/video_evidence.py`; contract:
`.claude/skills/simplicio-tasks/references/video-evidence.md`. A missing toolchain BLOCKS, never a
fake pass.

## Tests & local checks (no paid CI)

`python3 scripts/check.py` runs the whole gate locally: the `tests/` suite (worker `selftest`s + an
e2e of the loop driver proving it stops on EVIDENCE, ignores a bare `<promise>`, stops on the cap;
+ producers BLOCK, never fake-pass, when a toolchain is absent) and `scripts/claims_audit.py`
(referenced scripts exist · extension-point count consistent · cited commands run · `_bundle ≡
source`). Self-runs on bare python3 (no pytest needed); `pip install "simplicio-loop[dev]"` adds
pytest. Wire as a git pre-push hook to keep work honest with zero CI cost.

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

The marketplace install carries only the **lean `plugin/` subdirectory** (the 6 skills + the 5
wired hooks) — `.claude-plugin/marketplace.json` `source` points at `./plugin`, so the pip-only
assets (capture proxy `engine/`, token-monitor dashboard) are NOT copied into a user's
plugin cache. `plugin/` is generated from source by `python3 scripts/sync_plugin.py` (run it after
editing skills or a wired hook); `scripts/check.py` fails if `plugin/` drifts from source.

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

**Safety is enforced, not just described:** `hooks/action_gate.py` is a **fail-closed**
`PreToolUse` (Bash) / git-pre-push hook that BLOCKS irreversible ops (force-push, history rewrite,
mass-delete, destructive DDL, infra teardown) and secret-laden commits/pushes before they run
(exit 2) — Step 5 made mechanical. `python3 hooks/action_gate.py selftest` proves the ruleset.

Claude's native tools satisfy the extension points: sub-agents → `execute`, file tools →
`deterministic_edit`, the scheduler → `watcher`. Where `simplicio-runtime` is installed,
`simplicio-cli mcp register --client claude-code` binds them deterministically.

## Other runtimes

The same skills run on Codex, VS Code (Copilot), Cursor, Antigravity, Kiro, OpenCode, Gemini,
Aider, Hermes, and OpenClaw — see [`adapters/MATRIX.md`](adapters/MATRIX.md) and
[`AGENTS.md`](AGENTS.md) for the runtime-agnostic contract (48 extension points; the binding
lives in the host, never in the skill).
