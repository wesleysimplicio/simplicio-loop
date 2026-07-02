# Changelog

All notable changes to **simplicio-loop** are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); the project uses SemVer.

## [Unreleased]

## [3.21.0] — 2026-07-02

### Added
- Survey step upgraded to the mapper 0.14 flow-docs engine: `ask . <verb> <arg> --json` for
  low-token structured queries during triage (`impact` feeds dependency widening, `tests-for`
  picks affected tests, `callers` aids review), `sync . --check` + `drift . --check` as
  docs-staleness/spec-drift gates in the DoD pass, and `flows`/`survey`/`business`/`history`/`diff`
  as the producers for documentation tasks. doctor's mapper capability probe now checks the full
  inspect/handoff/ask/sync/drift surface.
- The survey step also exercises the mapper 0.13 surface: `simplicio-mapper inspect . --json`
  is the survey's own evidence gate (artifacts proven on disk before the loop trusts them) and
  `simplicio-mapper handoff . --json` feeds the goal with the compact context-pack
  (files/symbols/deps/`pack_hash` + `llm_directives`) instead of re-reading the tree
  (`.claude/skills/simplicio-loop/SKILL.md`, `simplicio-tasks` extension-points reference,
  `plugin`/`_bundle` mirrors).

### Changed
- Dependency floors raised to the current releases of the two bound operators:
  `simplicio-mapper>=0.14.0` and `simplicio-cli>=0.9.1` in `pyproject.toml`, so a fresh
  `pip install simplicio-loop` resolves the operators this release was verified against.

## [3.20.1] — 2026-07-02

### Added
- `.github/workflows/ci.yml` — runs `python3 scripts/check.py` (claims-audit + worker
  selftests/smoke) on every push/PR to `main`. The gate already existed locally; nothing ran it
  automatically before this — flagged as the highest-confidence finding of an ecosystem audit.

### Changed
- Dependency floors raised to the current releases of the two bound operators:
  `simplicio-mapper>=0.13.0` and `simplicio-cli>=0.9.0` in `pyproject.toml`, so a fresh
  `pip install simplicio-loop` resolves the operators this release was verified against.

## [3.20.0] — 2026-07-01

### Added
- **Cross-agent wiki now surfaces blockers and next actions** (`scripts/cross_agent_wiki.py` +
  `plugin`/`simplicio_loop/_bundle` mirrors). Watcher verification output was already rendered in
  the wiki; this extends the same render path to the blocker list and the recommended next-action
  set so a handoff reader sees what is stuck and what to try next, not just pass/fail status.
- **End-to-end test of the loop driver** (`plugin/tests/test_loop_e2e.py` + `_selfrun.py`,
  mirrored under `simplicio_loop/_bundle`) covering `hooks/loop_stop.py`'s evidence-gated stop,
  anti-false-done (a bare `<promise>` is ignored without evidence), and anchor-gate exit paths.
- `.claude/skills/simplicio-loop/SKILL.md` (+ `plugin` mirror) documents the spindle/latch
  cross-agent handoff pattern.

### Fixed
- **Spindle/latch state-file mismatch between `handoff.py` and `loop_stop.py`.** `loop_stop.py`
  had renamed `SPINDLE_STATE` to `spindle_state.json` with no compat shim, silently breaking the
  cross-agent handoff/latch feature — `spindle_latched()` and `spindle_active()` failed open to
  `False` when the expected file was missing. `scripts/handoff.py` now points `SPINDLE_STATE` at
  `spindle_state.json`, keeps a `LEGACY_SPINDLE_STATE` constant for the old `spindle.json` name, and
  `_read_spindle()` tries the new filename first before falling back to the legacy one — mirroring
  the existing `DONE_FLAG`/`LEGACY_DONE_FLAG` backward-compat pattern in `hooks/loop_stop.py`.

## [3.18.1] — 2026-06-30

### Fixed
- `references/orchestration.md` Step 3c still said "TRIVIAL/SMALL skip adversarial review; only
  MEDIUM+ pay it" — a leftover from before v3.17.0's 6-agent floor that directly contradicted
  Step 3's new policy a few lines above (found by an independent audit). Now states the review
  fan-out (roles 3-6) runs on every item regardless of tier; only Rubric C's heaviest sub-checks
  stay tier/surface-scoped.
- Deleted `.agents/skills/` — an untracked, gitignored local mirror that still carried the
  pre-v3.17.0 fast-path/solo policy a week stale; nothing referenced or synced it.

## [3.18.0] — 2026-06-30

### Changed
- **Native simplicio-runtime bind is now REQUIRED, not optional, on 8 of the 11 supported
  runtimes** (Claude Code, Codex, Cursor, VS Code, Antigravity, Kiro, OpenCode, Hermes —
  `FORCED_BIND_RUNTIMES` in `scripts/install_lib.py`). Previously every adapter's "Native bind"
  section was framed as an optional speed-up, and the installer only printed a suggestion line
  that never actually ran. `scripts/install_lib.py` gained `ensure_runtime_bind()`, called
  unconditionally for these 8 runtimes during install: it runs `simplicio install --global`
  (auto-registers Claude/Codex/Cursor/VS Code/Kiro's MCP config in one pass), fixes OpenCode's
  MCP registration, verifies Hermes/Antigravity's runtime health via `simplicio doctor --json`,
  and — if `simplicio` isn't installed at all — logs a loud, unmissable warning instead of a
  quiet "optional" note. Every generated entry file (`AGENTS.md`/`GEMINI.md`/
  `copilot-instructions.md`/Kiro steering) now states the requirement inline for these hosts.
- **Fixed a broken MCP invocation referenced across 5 adapter READMEs and the OpenCode
  auto-registration code.** `simplicio-cli mcp register --client <x>` doesn't perform MCP
  registration — `simplicio-cli` (simplicio-dev-cli) defers entirely to `simplicio install`. The
  correct, now-documented path is `simplicio install --global` (covers Claude/Codex/Cursor/
  VS Code/Kiro) or the manual JSON snippet pointing at `simplicio serve --mcp --stdio`. Four
  README JSON snippets (VS Code, Antigravity, Kiro, OpenCode) and `merge_opencode_mcp()` had the
  args backwards (`["mcp", "serve"]` instead of `["serve", "--mcp", "--stdio"]`) — fixed
  everywhere it appeared.
- Gemini, Aider, and OpenClaw are unaffected — native bind stays optional/native-by-design on
  those three, per the user's explicit scope for this change.

## [3.17.0] — 2026-06-30

### Changed
- **Minimum 6-agent orchestration floor (`simplicio-tasks`/`simplicio-review`).** Removed the
  solo/inline fast-path and the TRIVIAL/SMALL single-self-review shortcut. Every item, regardless
  of size, now runs a 6-role floor: orient+plan, implement, 3 parallel adversarial review rubrics
  (security/correctness, quality, does-it-reproduce — `simplicio-review`), and an independent
  blast-radius reviewer that re-checks `impact_audit.json`/`flow_audit.json` instead of trusting
  the implementer's self-reported scope. The Step 3 auto-scaling formula gained a `max(6, ...)`
  floor; when disk is too tight for 6 worktrees, the floor still runs under `isolation=shared`
  instead of dropping below 6. Rubric C (does-it-reproduce) is no longer gated to LARGE/CRITICAL —
  it runs always; only its heaviest sub-checks (web/flow evidence) stay scoped to diffs that touch
  that surface.

## [3.16.0] — 2026-06-30

### Added
- **Cross-agent handoff on incomplete stop (`hooks/loop_stop.py`).** A budget-halted or
  iteration-cap stop now writes `.orchestrator/loop/HANDOFF.md` before clearing the scratchpad —
  the frozen goal/acceptance criteria from `task_anchor.py`, the last attempts from
  `loop_journal.py`, and concrete resume steps. Previously these stops deleted the scratchpad with
  nothing durable left for a different agent/runtime to pick the task back up cold; a successful
  (promise-fulfilled) stop is unaffected and still needs no handoff.

## [3.15.0] — 2026-06-30

### Added
- **Task-scope impact gate (`scripts/impact_audit.py`).** The orchestrator can now audit the
  planned task surface before editing: local dependencies, reverse dependents/callers, and related
  tests are mapped from the seed files a task intends to touch. Uncovered reverse dependencies now
  block the plan by default, and `--fail-on medium` upgrades missing local deps/tests into blockers
  for shared/public contracts and signature changes.
- **Full-stack flow coverage gate (`scripts/flow_audit.py`).** The orchestrator can now map mixed
  frontend/backend/service workspaces into a UI-action → frontend-call → backend-endpoint →
  service-call graph, blocking objective integration gaps such as frontend calls with no matching
  backend endpoint and backend endpoints that still look stubbed/incomplete. Medium-confidence loose
  ends (buttons/actions with no observed backend call, orphan endpoints, local-looking service calls
  without local endpoints) must be explicitly classified or promoted to ACs for flows that promise
  backend integration.

### Changed
- `simplicio-tasks`, `simplicio-loop`, and `simplicio-review` now require impact-audit evidence
  before and during shared-file changes so callers/tests cannot stay outside the declared task
  surface by accident.
- `simplicio-tasks`, `simplicio-loop`, and `simplicio-review` now require flow-audit evidence for
  mixed front/back/service changes, with `--fail-on medium` for ACs that promise backend integration.

## [3.14.1] — 2026-06-29

### Changed
- **Declared the operator version floors required by the loop.** `simplicio-loop` now requires
  `simplicio-mapper>=0.11.0` and `simplicio-cli>=0.7.1` in `pyproject.toml`, matching the current
  first-party operator releases that the loop already expects for the async mapper survey and the
  current dev-cli/runtime contract. This makes fresh installs deterministic even before the
  preflight auto-update step runs.

## [3.14.0] — 2026-06-29

### Changed
- **The loop preflight now auto-updates the operators to their latest release.** Before iteration 1,
  `simplicio-loop` runs `python3 -m pip install -qU simplicio-mapper simplicio-cli` (with a PEP-668
  `--user --break-system-packages` fallback) so every run uses the newest mapper/dev-cli — no manual
  re-install needed to pick up a new `simplicio-mapper`. **Fail-open and offline-safe**: a
  network/pip failure (or a deliberate pin) leaves the installed build in place and the loop
  proceeds; a still-missing operator AFTER the attempt is the existing hard BLOCK. Runs once per
  loop preflight, not per turn. Updated `simplicio-loop/SKILL.md` (preflight block + phase table);
  mirrored into `plugin/` and `_bundle/`.

## [3.13.0] — 2026-06-29

### Changed
- **Wired the `simplicio-mapper` two-tier async survey (v0.9+) into the loop.** The `orient` /
  survey step now prefers `simplicio-mapper scan . --json` (instant `macro` skeleton + deep index
  in the background) over the blocking `index` crawl, polls `simplicio-mapper status . --json` for
  the deep-pass phase, and uses `macro . --json` for instant triage. `index . --json` is retained
  as the forced-synchronous build (`scan --sync`/`--await` for blocking waits). Updated
  `simplicio-loop/SKILL.md`, `simplicio-tasks/references/extension-points.md`, and the
  understand-anything adapter; mirrored into `plugin/` and `_bundle/`.

## [3.12.0] — 2026-06-29

### Added
- **Task anchor — durable working memory for SCOPE (`scripts/task_anchor.py`), the anti-deviation
  guard.** The loop already remembered its ATTEMPTS (`loop_journal.py`, the stall detector); it now
  also remembers what the TASK ACTUALLY IS. The anchor freezes the acceptance criteria once at
  intake and makes three things deterministic and model-free: `check` flags goal-drift each turn
  (exit 11 when the goal worked ≠ the frozen goal), `mark` records a per-AC receipt (a `done` with
  no evidence is refused), and `gate` (exit 12) blocks "done"/PR-open while any criterion is still
  unverified. Closes the "desvio de tarefas" complaint: the run can no longer silently narrow or
  wander off the task.
- **`pr_evidence` worker (`scripts/pr_evidence.py`) — every PR carries prints + an item-by-item AC
  check.** Assembles the PR body mechanically (never hand-written): the item-by-item
  acceptance-criteria checklist from the task anchor PLUS the screenshots/recordings captured by
  `web_verify`/`video_evidence` under `.orchestrator/tee/web`. With `--require-evidence` it FAILS
  CLOSED (exit 3) rather than open a PR that has neither a checklist nor a print, and it honors a
  discovered `.github/PULL_REQUEST_TEMPLATE.md`. Closes the "PR opened without prints / without an
  item-by-item check of the task" complaint.

### Changed
- **The anti-drift gate is now ENFORCED, not just documented.** `hooks/loop_stop.py` and
  `hooks/loop_capture.py` read the task anchor directly (self-contained — no dependency on
  `scripts/`, which the lean plugin does not ship) and reject a completion `<promise>` while any
  acceptance criterion is still unverified, exactly as they already reject a promise with no
  in-turn evidence. Fail-open: a missing/unreadable/empty anchor never blocks, and any rejection
  stays bounded by `max_iterations` + the budget kill-switch. The re-feed header now names the open
  criteria so the next turn knows what blocks "done".
- Wired both workers into the skills (`simplicio-tasks` Steps 2b/4/6, `simplicio-loop` triage +
  evidence-gated promise) and the `delivery_gate` / `pr_template` extension points.

## [3.11.0] — 2026-06-26

### Removed
- **The vendored native performance core and all its modules** (`simplicio-core` / `-py` /
  `-proxy` / `-parity`) — removed in full, along with their build/parity tooling.
- **The ported ONNX model engine modules and their subcommands** — `kompress`, `router`, `embed`,
  and `image` (`engine/simplicio_kompress.py`, `engine/simplicio_router.py`,
  `engine/simplicio_embed.py`, `engine/simplicio_image.py`) — plus the **GitHub Copilot OAuth port**
  (`copilot`, `engine/simplicio_copilot.py`).
- **The `[onnx]` and `[kompress]` optional-dependency extras** and the ONNX-model `doctor` checks
  (onnxruntime / huggingface_hub / tokenizers / pillow are no longer pulled in or probed).
- **All references to and integration with the external compression tool.** The native, stdlib-only
  Simplicio capture engine (deterministic compression) is now the sole engine.
- **Rust as a referenced target stack.** Dropped all Rust/ecosystem references project-wide —
  the language-detection rule and `.rs` signature mapping in the engine, the `cargo*` entries in
  the output-clamp allow-list (`hooks/orient_rewrite.py`), and every `cargo`/crate example across
  the README + 13 translations, the skills, and the docs. Build/test/lint examples now use the
  remaining stacks (node / python / go). The separate external `simplicio-runtime` is unaffected.

## [3.10.3] — 2026-06-26

### Changed
- **`PreToolUse` (Bash) hooks are now project-scoped** — `action_gate.py` and `orient_rewrite.py`
  fire only inside an active simplicio-loop project (an `.orchestrator/` marker in cwd/an ancestor,
  or `SIMPLICIO_LOOP=1`); elsewhere they no-op so the command runs unchanged. The home directory is
  never treated as a project, so a stray `~/.orchestrator` cannot widen the scope. This clears the
  marketplace-scanner `has_broad_scope_hooks` finding (the gate no longer intercepts Bash globally)
  while preserving the full fail-closed behavior inside a real run. Verified: outside a project a
  `git push --force` is a no-op (exit 0); inside, it is BLOCKED (exit 2).
- **Honest, behavior-matching descriptions** — `plugin.json` and the marketplace entry now disclose
  both project-scoped PreToolUse Bash hooks (the fail-closed safety-gate and the opt-in read-only
  output-clamp/rewrite) plus the Stop loop/learn hook, and that it runs 100% locally with no telemetry
  or network calls. Removed the unverifiable "up to 96% fewer tokens" headline from the plugin copy
  (that figure is the capture proxy's, which the plugin does not ship); the savings line remains
  evidence-gated. Clears the scanner `description_matches_behavior` finding.
- **Marketplace `category` corrected** `developer-tools` → `development` (a valid directory category).

### Fixed
- **Capture wiring no longer breaks the `claude` CLI.** The economy installer wired
  `ANTHROPIC_BASE_URL=http://127.0.0.1:<proxy>` into the shell profile unconditionally. Claude Code /
  the `claude` CLI authenticate via OAuth, and the proxy cannot relay that token — Anthropic returns
  `401 Invalid authentication credentials`, so every terminal `claude` call failed (the proxy still
  *captured* the request, but the call broke). `ANTHROPIC_BASE_URL` is now wired **only when a static
  `ANTHROPIC_API_KEY` is present** (the one case the proxy can forward); otherwise it is skipped and
  any stale proxy-pointing value is removed on the next `wire`. Fixed consistently in
  `scripts/simplicio-economy.sh`, `scripts/install_services.py`, and `scripts/setup_simplicio.sh`.
- **`doctor.py` no longer false-WARNs on OAuth setups.** The `always-capture wire` check required
  `ANTHROPIC_BASE_URL` to point at the proxy; it now requires that only when a static
  `ANTHROPIC_API_KEY` is set, and treats "OpenAI wired + Anthropic absent" as healthy for OAuth users.

## [3.10.2] — 2026-06-25

### Fixed
- **Skill frontmatter now parses under strict YAML** — `simplicio-learn` and `simplicio-loop` had a
  bare `: ` (colon-space, e.g. `memory lean: durable`, `Runtime-agnostic: binds`) inside an unquoted
  `description:` scalar, so `claude plugin validate --strict` (Anthropic's submission validator)
  rejected them and a strict parser dropped all metadata. Both descriptions are now double-quoted
  (text unchanged). `claude plugin validate ./plugin --strict` and `… validate . --strict` both PASS.
- Moved the `category` field from `plugin.json` to the `marketplace.json` plugin entry (where the
  validator expects it) — clears the last strict-validation warning.

### Added
- **`video_evidence` Playwright engine (now the DEFAULT)** — the normal evidence flow records the
  REAL browser session driving the screen (`video_evidence verify --url …` → `.webm`, → `.mp4` with
  FFmpeg) as the "works, not just compiles" moving proof. **hyperframes** is now used ONLY for an
  EXPLICIT custom request ("make an explainer video of screen X") — a deterministic captioned
  slideshow. New `record` verb + `--engine hyperframes` selector; both BLOCK (never fake-pass) when
  their toolchain is absent. New smoke test (suite 27 → 28).
- **Lean marketplace plugin** (`plugin/` subdirectory) — `.claude-plugin/marketplace.json` `source`
  now points at `./plugin`, a slim tree carrying ONLY the 6 skills + the 5 wired hooks. The pip-only
  assets (capture proxy `engine/`, token-monitor dashboard, `scripts/`) are no longer copied
  into a user's plugin cache on install — smaller downloads and a cleaner submission. Generated by
  `scripts/sync_plugin.py`; `claims_audit` gained a **plugin-parity** check (now 5/5).

### Changed
- video_evidence contract + all docs (extension-points, CLAUDE.md, SKILL.md Step 4b, README +
  15 translated READMEs) reframed to the two-engine model (Playwright default · hyperframes on request).
- Translated READMEs reconciled to **v3.10.1** (several were stale at v3.4.0) and the test-count badge
  to 28; stale Mermaid `video_evidence (hyperframes MP4)` nodes updated.

### Removed
- The **Open-core billing** capability-table row (and its `PRICING.md` link) from `README.md` and all
  15 translated READMEs — no money/pricing mention remains in any README.

## [3.10.0] — 2026-06-25

### Added
- **`repo_conventions` worker** (`scripts/repo_conventions.py`; verbs `learn`/`show`/`branch`/`commit`/`selftest`):
  learns the repo's OWN playbook by mining git history (branch scheme, commit convention + the real
  scope list, ticket pattern — by frequency) + merged PRs via `gh` + static config (CONTRIBUTING/AGENTS/
  pyproject for a Conventional-Commits hint, PR template for section structure) into a hash-pinned
  `.orchestrator/conventions.json`. Confidence-gated: a sparse/inconsistent history degrades to an honest
  Conventional-Commits default, never an over-fit guess. Steps 4–6 shape branch/commit/PR names
  deterministically from it. Wired into `simplicio-tasks` Step 1a'/Step 3/Step 6,
  `references/extension-points.md`, `references/orchestration.md`; added to `claims_audit`
  `SELFTEST_SCRIPTS` + `tests/test_worker_smoke.py` (selftest 19/19).
- **Worktree-per-item isolation is now the DEFAULT** (one `git worktree` per item → zero cross-item
  conflict); a shared checkout is the opt-out for big compiled modules.
- **Genuine fast-path:** a single interactive item no longer auto-arms the loop (no scratchpad → the
  stop-hook lets the turn end). The loop engages only for a real body of work (queue / drain / 24-7).
- **Stop-hook background-gate awareness** (`hooks/loop_stop.py`): a fresh `.orchestrator/loop/gate.lock`
  marks "waiting on a background gate (verification workflow / CI / long task)" so the hook does NOT
  re-fire as an idle turn; the lock is TTL-bounded (30 min) so a stale lock can never trap the loop
  (fail-open).
- README: a **Join the Simplicio Discord** badge (`https://discord.gg/wM6tr7xVb`) in the top badge row.

### Changed
- **i18n:** every user-facing usage example / command string translated from Portuguese to English
  across the 6 skills, the `_bundle` mirror, `README.md` + 15 translated READMEs, `CLAUDE.md`/`AGENTS.md`,
  `scripts/video_evidence.py`, and tests. Localized PROSE inside the translated READMEs is intentionally
  kept in-language; the `video_evidence` detector stays multilingual (EN/PT/ES).
- **CLI naming:** the unified CLI is now invoked as **`simplicio-cli <command>`** instead of the bare,
  colliding `simplicio` — all doc/skill references plus the `bin/simplicio` launcher (→ `bin/simplicio-cli`)
  and the engine help/branding were updated. (The PATH-binary rename itself lives in the separate
  `simplicio-cli` package.)
- README (all 15 languages): the **Claude Code / Cursor** install path no longer uses the
  marketplace plugin (`/plugin marketplace add` … `/plugin install simplicio-loop@simplicio`). It now
  installs **straight from the latest GitHub release** — `gh release download --archive tar.gz` → `tar
  xzf` → `bash scripts/install.sh claude|cursor` (verified to resolve the latest source tarball).

### Fixed
- Reconcile the extension-point count after #53 (44 → **48**): the badge/anchor/table in `README.md`,
  the runtime-contract line in `CLAUDE.md`, and the depth-table row in `simplicio-tasks/SKILL.md`
  still said 44 while `extension-points.md`/`AGENTS.md` already said 48. Re-synced the pip bundle
  (`simplicio_loop/_bundle/skills/*`) to source so `scripts/check.py` is green again (audit 4/4).

### Removed
- The **`## 💳 Pricing`** section (heading + open-core/billing-proposal paragraph) from `README.md`
  and all 14 translations under `READMEs/`. The capability-table billing row and `PRICING.md` are
  left untouched.

## [3.9.3] — 2026-06-24

### Fixed — `video_evidence` renders again on the shipped hyperframes (0.7.x)
- The `video_evidence` worker (`scripts/video_evidence.py`) emitted a composition keyed off
  invented `data-hf-*` attributes and invoked `hyperframes render --input <file> --output <file>` —
  neither matches hyperframes 0.7.x, so every demo-video render failed (`FAIL — mp4 (not
  produced)`). Rewrote the composition to the real schema (`#root[data-composition-id]` + timed
  `.clip[data-start/data-duration/data-track-index]` elements gated by the framework, faded in via a
  paused GSAP timeline registered on `window.__timelines`); the project entry point is now
  `index.html`. Render now passes the **project dir** positionally (`hyperframes render <dir> -o
  <mp4> -f <fps> -q <quality>`).
- Screenshots are copied into `<project>/assets/` instead of referenced by a `../..` path, because
  hyperframes serves the project over a local file server during render — an out-of-tree asset path
  never resolved. Captions are humanized from the shot filename (ordering prefix stripped, acronyms
  kept). The missing-toolchain / missing-composition **BLOCK** (never fake-pass) behavior is
  unchanged — `python3 scripts/check.py` stays green (claims-audit 4/4 · 24 tests).

## [3.9.2] — 2026-06-24

### Changed — release sync (no functional changes)
- Maintenance bump that re-cuts the package so the local and global installs carry a matching
  version after the 3.9.x Token Monitor rebrand and `simplicio-loop dashboard` work. Keeps the
  three version sources in lockstep (`pyproject.toml`, `.claude-plugin/plugin.json`, and the
  `simplicio_loop/__init__` fallback literals) so there is no version drift across the pip
  artifact, the marketplace plugin, and the bundled skills/hooks.

## [3.9.1] — 2026-06-24

### Fixed — `simplicio-loop` is typeable on PATH (so `simplicio-loop dashboard` actually works)
- A `--user` pip install can drop the `simplicio-loop` console-script in a dir that isn't on PATH
  (macOS `~/Library/Python/X.Y/bin`, Windows `%APPDATA%/Python/*/Scripts`). The installer now symlinks
  it into `~/.local/bin` (same treatment the two loop operators already get), so the documented
  re-open command `simplicio-loop dashboard` is typeable on all three OS — not just runnable by full
  path. Generalized `_link_operator_bins` → `_link_console_script(name)`; best-effort, idempotent.

## [3.9.0] — 2026-06-24

### Added — `simplicio-loop dashboard` (open the Token Monitor from anywhere)
- **`simplicio-loop dashboard`** — a console subcommand that starts the bundled Token Monitor server
  (detached, if it isn't already up) and opens the browser. Works from anywhere after a `pip install`
  — no repo checkout or path needed. Flags: `--port` (default 9090), `--no-browser` (start the server
  only), `--stop` (close a running monitor). This is the easy way to re-open the dashboard — type the
  command, or just ask the agent ("open the token dashboard").

### Changed — dashboard opens once on install, then stays on-demand (never forced open)
- A **fresh install opens the dashboard once** so you see it works, then it's on-demand. Guarded by a
  marker (`~/.simplicio/.dashboard_shown`): a re-install/update **never reopens** it. Opt out with
  `SIMPLICIO_NO_DASHBOARD=1` (and `SIMPLICIO_NO_BROWSER=1` to skip just the browser). The first-run open
  is best-effort and **never blocks the install** (detached, headless-safe). Only the capture proxy stays
  always-on; nothing is forced to stay open.

### Fixed — package `__version__` drift
- `simplicio_loop.__version__` was hardcoded at `1.0.3` while the package shipped 3.x — `simplicio-loop
  --version` now reads the real version from the installed package metadata (single source of truth).

### Fixed — cross-platform correctness (macOS · Windows · Linux), adversarially reviewed
- **Windows:** the dashboard PID file no longer hardcodes `/tmp` (which doesn't exist on Windows and
  crashed the server on start) — both `simplicio_loop/cli.py` and `hooks/simplicio_dashboard.py` use
  `tempfile.gettempdir()` (→ `%TEMP%` on Windows, `/tmp`/`$TMPDIR` on Linux, `/var/folders/…` on macOS),
  and the PID write is now defensive (creates the dir, never fatal). `--stop` works on all three OS
  (PID-file `os.kill` everywhere; `pkill` is an extra fallback only where it exists).
- **Headless Linux:** `webbrowser.open()` is only called when a GUI is actually present
  (`DISPLAY`/`WAYLAND_DISPLAY`, or macOS/Windows) — on a headless box it would otherwise launch a
  text browser that inherits stdin and **blocks forever** (a hang `try/except` can't catch). Install
  now never blocks on a headless/CI machine; the first-run open is simply skipped there.
- Hardened `sys.executable or "python3"` consistently so a `None` interpreter can't crash the launcher.

## [3.8.0] — 2026-06-24

### Changed — release hygiene + PyPI publish
- **Published to PyPI** (`pip install -U simplicio-loop` now resolves this version). Re-synced the
  shipped bundle (`simplicio_loop/_bundle/hooks/simplicio_dashboard.py`) so the pip artifact carries
  the current 10-runtime neon dashboard byte-for-byte (`bundle ≡ source`, audited by
  `scripts/claims_audit.py`).
- **Fixed the plugin manifest version drift:** `.claude-plugin/plugin.json` was stuck at `3.3.0`
  while `pyproject.toml` advanced — both are now synced at the release version.
- `build/` (setuptools build artifacts) is git-ignored.

## [3.7.0] — 2026-06-24

### Added — `scripts/doctor.py` (verify + `--repair`); optional pieces never block
- **`python3 scripts/doctor.py [--repair]`** (also `bash scripts/simplicio-economy.sh doctor`) checks
  the whole stack and cleanly separates **REQUIRED** (python3, the two loop operators, the 6 skills,
  the loop hooks + Stop wire, the always-on capture proxy — `--repair` installs/wires them) from
  **OPTIONAL** accelerators (the ONNX models backend, the **native performance core**, the menu-bar tray dep).
- **Missing an OPTIONAL piece is never a failure and never blocks.** If a user doesn't have the native
  performance core (or onnxruntime, or the tray dep), doctor reports it as `○ optional` and the **exit code stays 0** as
  long as every REQUIRED item is healthy — the Python engine + the deterministic path cover everything.
  Verified: simulating an absent native performance core still exits 0. `--repair` is PEP-668-robust and best-effort
  on optionals (it tries to install them but won't fail the run if it can't).
- The installer now points at it: `python3 scripts/doctor.py --repair` after install. README documents it.

## [3.6.0] — 2026-06-24

### Changed — dashboard + tray are ON-DEMAND (only the capture proxy is always-on)
- **The Token Monitor dashboard and the menu-bar tray no longer auto-open or stay running.** Only the
  **capture proxy** auto-starts (the wired clients need it reachable); it captures + measures in the
  background. Open the UI only when you want:
  - `bash scripts/simplicio-economy.sh monitor` — start the dashboard + open the browser · `monitor stop` to close.
  - `bash scripts/simplicio-economy.sh tray` — start the menu-bar tray · `tray stop` to close.
- **Installers updated**: `setup_simplicio.sh` (macOS launchd) now registers **only** the proxy and
  removes any leftover monitor/tray auto-start plists; `install_services.py` (Linux systemd · Windows
  Startup) auto-starts only the proxy via a new `AUTOSTART` set and sweeps old monitor/tray units;
  `install_lib.py` prints the on-demand commands. README + `simplicio-economy.sh status` reflect the
  new model (`status` shows the dashboard/tray as on-demand).

## [3.5.0] — 2026-06-24

### Changed — install is COMPLETE by default (everything is mandatory)
- **`scripts/install.sh <runtime>` now installs the whole stack with no flags** — the user gets
  everything, not opt-in pieces: the two loop operators, the **full Python stack** (the package +
  the `[onnx]` models backend — onnxruntime + huggingface_hub + tokenizers + pillow), the 6 skills +
  hooks with the Stop hook wired, AND the always-on Token Monitor (capture proxy + dashboard `:9090`
  + tray) with Claude + Codex + Hermes routed and measured. The old `--with-monitor` opt-in is gone
  (now default). The only opt-out is **`--minimal`** (alias `--no-monitor`) for headless/CI.
- `install_lib.py`: new `install_all_deps()` (PEP-668-robust, fail-open) installs the package + ONNX
  extras + tray dep; `setup_monitor` is default-on and registers services + runs `wire`.
- **Verified on this machine**: a full `install.sh claude --global` left onnxruntime 1.27 + hf +
  tokenizers + pillow + rumps + the `simplicio-loop` package installed, 6/6 skills, both operators on
  PATH, 3/3 services running, monitor live, and `Claude ✓ · Codex ✓ · Hermes ✓` measured.

### Fixed
- `setup_simplicio.sh` service registration is now **idempotent on re-install** — bootout is async,
  so it waits before bootstrap and falls back to `kickstart -k` when the service is still loaded
  (fixes the `Bootstrap failed: 5: Input/output error` on re-run). Restored all 10 runtimes in the
  dashboard coverage panel (a prior change had trimmed it to 7).

## [3.4.1] — 2026-06-24

### Added — `scripts/update.sh` (one-command update)
- **`bash scripts/update.sh [<runtime>]`** — pulls the latest (`git pull --ff-only`, auto-stashing
  local edits and restoring them), reinstalls skills/hooks/operators from the fresh source, restarts
  the always-on services (launchd on macOS · systemd on Linux) so they run the new code, and prints
  the live stack + savings. Verified end-to-end on macOS.

### Fixed — installer robust on externally-managed Python (PEP 668)
- **`install_lib.py ensure_operators`** now installs the loop operators (`simplicio-mapper` +
  `simplicio-cli`) even on Homebrew/Debian Python: it retries `pip install --user --break-system-packages`
  on a PEP-668 failure, then **symlinks the console-scripts into `~/.local/bin`** (the `--user` scheme
  can drop them in a dir off `PATH`, e.g. macOS `~/Library/Python/X.Y/bin`). Without this, the operators
  installed but weren't on `PATH`, so the loop drive would block. Documented `--with-monitor` and the
  update flow in the README Install section.

## [3.4.0] — 2026-06-24

### Added — final loop-orchestrator hardening (the last gaps to 10/10)
- **`hooks/action_gate.py` — a FAIL-CLOSED safety gate, Step 5 made mechanical.** Runs as a Claude
  `PreToolUse` (Bash) hook AND/OR a git pre-push hook and BLOCKS (exit 2), *before* the command runs:
  force-push / history rewrite (`filter-branch`), remote-ref deletion, mass-delete (`rm -rf /`),
  destructive DDL (`DROP DATABASE`/`TRUNCATE`), infra teardown (`terraform destroy`), and any
  commit/push whose **staged diff contains a secret** (AWS/GitHub/Slack/OpenAI keys, private keys,
  hardcoded credentials — placeholder-aware). Benign commands pass untouched; a push whose diff
  can't be scanned is blocked (a safety check that can't run is not a pass). `selftest` 14/14;
  pytest `tests/test_action_gate.py`. Wired into `hooks.claude.json` + the hooks README.
- **Incremental triage — `loop_journal.py since`.** Each record now stamps the HEAD commit; `since`
  shows only the delta (diff-stat + working tree) since the last recorded turn, so a turn reads what
  changed instead of re-scanning the whole tree every iteration.
- **Two loop modes — `converge` vs `drain`.** The scratchpad `mode` selects termination logic:
  `converge` (single hard task — ends on the evidence-gated promise or a stall escalation) vs
  `drain` (a queue — ends when the source re-query stays empty K rounds). Documented in
  `simplicio-loop` SKILL so the two dynamics aren't conflated.

### Added — tests + claims-audit + local check runner (turn assertions into proof; no paid CI)
- **`tests/` suite** — the workers' deterministic `selftest`s, an **e2e of the loop driver**
  (`hooks/loop_stop.py`) proving it stops on EVIDENCE, ignores a bare `<promise>`, and stops on the
  cap as *distinct* exits, and smoke tests proving the evidence producers **BLOCK (never fake-pass)**
  when their toolchain is absent. Runs under `pytest` **or** self-runs on bare python3
  (`tests/_selfrun.py`) — no pip required. Verified: 12 passed under pytest and the stdlib fallback.
- **`scripts/claims_audit.py`** (fail-closed) — every `scripts/*.py` referenced in the docs exists ·
  the extension-point count agrees across all files · each cited worker command runs · the shipped
  `simplicio_loop/_bundle/` skills are byte-identical to source. **Caught real drift on first run**
  (the bundle was missing 5 reference files); repaired to an exact mirror.
- **`scripts/check.py`** — one local gate (`claims_audit` + tests), wireable as a git pre-push hook.
  Replaces a paid GitHub Action: the verification runs on the dev's machine at zero CI cost.
- `pyproject.toml` `[dev]` extra adds pytest (convenience only). Docs: README (✅ Tests section),
  AGENTS, CLAUDE.

### Changed — savings line is now evidence-gated, not mandatory (honesty fix)
- **Removed the "end every message with the mandatory savings line" obligation.** That rule forced a
  figure even with no `savings_ledger` bound, which pressured fabricated baselines/percentages.
- **New rule (mirrors the `<promise>` evidence gate):** emit a savings line ONLY when a turn actually
  ran an economy-producing command and the number traces to a **measured receipt** — `orient_clamp`
  tee (bytes/lines saved), signatures-only read (lines saved), native cache hit (call skipped),
  `deterministic_edit` (0 edit tokens), or the capture proxy / `savings_ledger` / `savings_harness
  score`. No measured economy → **no savings line**; never fabricate a baseline or a %.
- A baseline `%` may be quoted only when an actual control arm was run+measured (`savings_harness`),
  never estimated from memory. Updated `simplicio-tasks`/`simplicio-loop` SKILLs, AGENTS, README;
  `_bundle` synced.

### Added — loop attempt-memory + stall detector (`scripts/loop_journal.py`)
- **Durable run-journal** `.orchestrator/loop/journal.jsonl` (append-only: `iteration`, `action`,
  `hypothesis`, `gate`, error `fingerprint`) — the loop's working memory of WHAT WAS TRIED, beside
  the scratchpad's WHAT (the goal). Closes the two failure modes of a memoryless re-feed loop:
  re-deriving the same triage every turn, and **oscillation** (try X → fail → try X again).
- **Stable error fingerprint** — failing gate output is hashed with line numbers, paths, hex/uuids,
  timestamps and durations normalized away, so the SAME bug is recognized across turns.
- **Stall detector** — `loop_journal.py stall`: STALLED when the last K consecutive attempts share
  the same fingerprint (default K=3); names the dead-end actions and recommends `switch-strategy`
  (K) or `escalate` (>K), `--exit-code` 10 for hook/`if:` gating. `resume` prints the anti-oscillation
  read at the top of each turn. Deterministic + model-free; `selftest` proves it (9/9, no files).
- Wired into `simplicio-loop` (loop contract steps 2–4, new § Run-journal + stall detector, the
  "good loop" criteria) and `simplicio-tasks` (Step 4 quality loop). Docs: README, AGENTS, CLAUDE;
  `_bundle` synced.

### Added — billing aggregator for the open-core paid tier (`scripts/billing_aggregator.py`)
- **Deterministic, model-free, privacy-preserving meter→invoice** over the metering records the loop
  already produces (`loop-budget.json`, `savings/snapshots.jsonl`, `trajectory/*.jsonl`,
  `tee/video/ledger.txt`). Verbs: `collect`/`meter`/`invoice`/`export`/`rates`/`selftest`.
- **Privacy boundary**: the savings snapshots store raw baseline/treatment TEXT; `collect` counts
  tokens (`ceil(chars/4)`) then **discards** the text — usage records carry counts only, never code,
  diffs, or rendered videos. **Fail-safe**: `invoice --prepaid` over-balance maps to the existing
  kill-switch `state: "halted"` (never over-serves). `selftest` proves the arithmetic (11/11, no files).
- Three price levers: per-seat (Pro), per-run (Team, one delivered+merged item), metered (Cloud:
  token passthrough + markup, render-minutes, operator-minutes). Implements the `PRICING.md` sketch.

### Added — demo-video creation + evidence via hyperframes (`video_evidence`, extension point #44)
- **`video_evidence` extension point** — binds [hyperframes](https://github.com/heygen-com/hyperframes)
  (HeyGen): renders HTML/CSS compositions to a **deterministic MP4** ("same input, same frames, same
  output"). Two jobs: (1) fulfil an explicit request — `/simplicio-tasks make a demo video of screen
  X` routes the work-item to the producer; (2) act as a CI-reproducible "works, not just
  compiles" proof for a UI change and a valid evidence-gated `<promise>` for the loop.
- **Worker** `scripts/video_evidence.py` — five verbs (`detect`/`scaffold`/`lint`/`render`/`verify`).
  `detect` classifies the request in-terminal (EN/PT/ES regex, no LLM); `verify` scaffolds a
  hyperframes composition from the `web_verify` per-step screenshots and renders the MP4 under
  `.orchestrator/tee/video/`. Missing toolchain (Node 22+, FFmpeg, hyperframes) → **BLOCKED**, never
  a fake pass. Chains after `web_verify` (Playwright captures the screens; hyperframes assembles them).
- **Contract** `.claude/skills/simplicio-tasks/references/video-evidence.md`; wired into
  `simplicio-tasks` (Step 2b routing + Step 4b evidence) and `simplicio-loop` (in-turn evidence
  producer). Extension-point count 43 → 44; skills/accelerators 10 → 11. Docs updated: README (EN +
  pt-BR), AGENTS.md, CLAUDE.md. Bundled skills under `simplicio_loop/_bundle/` synced.

## [3.3.0] — 2026-06-24

### Added — automatic capture routing for Claude + Codex (the monitor now measures all three)
- **`simplicio-economy.sh wire` now routes Claude (Anthropic) AND Codex/OpenAI through the capture
  proxy**, not just OpenAI — so the Token Monitor measures **Hermes + Claude + Codex** with no manual
  step. It sets `ANTHROPIC_BASE_URL` (no `/v1` — Claude appends `/v1/messages`) and `OPENAI_BASE_URL`
  (`/v1`) in the shell profile; `install_services.py wire` does the same cross-platform (`setx` on
  Windows). The engine routes each model to its **real** provider (`claude→anthropic`, `gpt→openai`,
  `deepseek→deepseek`) — **no model swap**. `setup_simplicio.sh` runs `wire` at install, so it is
  automatic.
- **Verified live**: through the proxy, an unauth'd `claude-3-5-sonnet` request returned Anthropic's
  own auth error + `request_id`, and a `gpt-4o-mini` request returned OpenAI's 401 — proving
  transparent forwarding to the real providers. `status` now shows `Claude ✓ · Codex/OpenAI ✓ · Hermes ✓`.
- **Idempotent · reversible · opt-outable**: re-running `wire` doesn't duplicate; `unwire` deletes the
  proxy routing deterministically (fixed a bug where a re-wire could poison the backup); a pristine
  `~/.zshrc.simplicio-bak` is kept; `SIMPLICIO_NO_WIRE=1` skips wiring entirely.

## [3.2.3] — 2026-06-24

### Changed
- README: added the "Running `simplicio-tasks`: economy vs measurement (per runtime)" subsection —
  economy applies on every runtime; measurement only counts traffic routed through the capture proxy.

## [3.2.2] — 2026-06-24

### Changed
- Synced all 14 translated READMEs to the comprehensive English README (capture-engine commands, ONNX
  models, native performance core, Token Monitor, corrected token-economy table). Tracked the project `.codex/` config.

## [3.2.1] — 2026-06-24

### Changed
- Comprehensive, transparent English README: documented the full capture-engine command surface (16
  commands), the 4 optional ONNX models, and the 4 native performance modules.

## [3.2.0] — 2026-06-24

### Added — the two token-economy techniques the README claimed but didn't implement, now real (2 agents)
- **Signatures-only reads** — `engine/simplicio_signatures.py` + `simplicio signatures <file>`: an
  `ast`-based skeleton view (imports, class/def signatures, first docstring line, top-level consts;
  bodies stripped to `...`), regex fallback for js/ts/go/java/…. Verified: `simplicio_dashboard.py`
  **870 → 65 lines (93% saved)** with every `def`/`class` preserved and no body leakage. Saves the
  tokens to read+navigate code.
- **Native response cache** — `engine/simplicio_cache.py`, wired into the capture proxy: a repeated
  **deterministic** request (`temperature == 0`, non-streaming) is served byte-exact from disk and the
  upstream LLM call is **skipped entirely → ~100% token saving on the hit**. Content-addressed key
  ignores volatile fields (`stream`/`user`/ids); LRU-bounded (500 entries / 50 MB); never caches 4xx or
  streaming/temp>0. On by default (`SIMPLICIO_CACHE=0` to disable). Verified end-to-end: an identical
  second request returned `X-Simplicio-Cache: HIT` with **zero** upstream calls. This also makes the
  dashboard's `cache_hit_pct` real (it was always 0). `simplicio cache stats|clear`.

### Changed
- README token-economy table corrected to reality: `CAP_TREE=100` → the real caps
  (`CAP_ERRORS/CAP_WARNINGS/CAP_LIST`); the `LMCache KV cache` row (an *external* optional accelerator,
  never built-in code) replaced by the now-implemented **native response cache**; signatures listed as
  the real `simplicio signatures` tool.

## [3.1.0] — 2026-06-24

### Added — the last two native performance modules (the port is now literally complete: every module)
- **the native passthrough proxy** — the upstream engine's native transparent reverse proxy,
  vendored + rebranded (zero residual upstream branding). **Built (40.8 MB
  binary) and verified running**: it forwarded a request to a local upstream byte-exact, preserved +
  injected headers (`x-forwarded-*`, `x-request-id`), rewrote `host`, and `/healthz` →
  `{"ok":true,"service":"simplicio-proxy"}`. **227 lib unit tests pass.**
- **the native parity harness** — the upstream engine's native-vs-Python parity harness, vendored
  + rebranded + built (`parity-run` binary, 7 transforms). **4 parity tests pass.**
- (Honest: the proxy's 50 *integration* test binaries couldn't finish linking here — disk-full, ~200 MB
  free; each statically links the ~40 MB ONNX/AWS tree. The release binary + lib tests built and passed.)

### Done — every subsystem AND every module of the upstream compression project is now in Simplicio
All four native performance modules (`simplicio-core` / `-py` / `-proxy` / `-parity`) build; the full Python functional
surface runs; the four real ONNX models (kompress / technique-router / MiniLM / SigLIP) run; Copilot
OAuth works. **the upstream port: complete.**

## [3.0.0] — 2026-06-24

### Added — the native performance core, built for real (the last literal piece)
- **the native performance core** — the upstream engine's native performance modules
  (the core + the native bindings), **vendored and rebranded** to simplicio
  (~70 source files: smart_crusher, diff/log/search compressors, tokenizer, relevance, CCR, content
  detection), Apache-2.0 with `NOTICE` crediting upstream. The rebrand is baked into the compiled
  binary, not cosmetic (`hello()` → `simplicio-core`, tag sentinel `{{SIMPLICIO_TAG_…}}`, env
  `SIMPLICIO_*`).
- **It builds and runs.** The native build produced a real wheel
  (`simplicio_core-…-abi3-…arm64.whl`); `import simplicio._core` works and real functions run
  (`LogCompressor` 5700→566 bytes, `SmartCrusher`, `DiffCompressor`, `detect_content_type` via magika).
  **The full native test suite passes.**

### Milestone — the upstream port is complete (capability + the native layer)
Every subsystem of the upstream compression project is now in Simplicio: the full Python functional
surface (deterministic + extractive compression, the **four real ONNX models** kompress /
technique-router / MiniLM embedder / SigLIP image, content detection + smart routing, RAG, input+output
capture, per-provider routing, MCP, CCR memory, init/wrap/report/verify/audit/capture/evals, copilot
OAuth) **and** the native performance core. The upstream port: done. (Skipped only the upstream's
native passthrough proxy that duplicates the working Python proxy — and its parity test
harness; both are non-capability.)

## [2.12.0] — 2026-06-24

### Added — Copilot OAuth (the last functional subsystem)
- **`simplicio copilot {login|token|status|logout}`** (`engine/simplicio_copilot.py`, stdlib) — GitHub
  Copilot OAuth **device flow** + Copilot token exchange, so Copilot CLI traffic can be routed through
  the capture proxy. Verified **live against the real GitHub API**: the device-code handshake returned a
  real `device_code`/`user_code`/`verification_uri`, and the poll returned the expected
  `authorization_pending`; `status`/`logout`/empty-store paths verified; token stored 0600 under
  `~/.simplicio`. (Honest: the post-auth Copilot token exchange can't be exercised here without a real
  Copilot account; the code path mirrors upstream exactly.)

### Milestone — capability-complete
With Copilot auth, **every functional subsystem of the upstream compression project is now ported to
Simplicio** and verified: all deterministic + extractive compression, the **four real ONNX models**
(kompress / technique-router / MiniLM embedder / SigLIP image), content detection + smart routing, RAG
(TF-IDF + embedding), input+output capture, per-provider routing, MCP, CCR memory, client init, wrap,
report, verify, audit, capture, evals, and copilot-auth. The **only** thing not reimplemented is the
upstream's native performance core — a **native performance** re-implementation of the Python (its parity harness
asserts native == Python), which adds **no new capability**, only native speed.

## [2.11.0] — 2026-06-24

### Added — image compression (the 4th and last real upstream model)
- **`simplicio image <path>`** (`engine/simplicio_image.py`) — vision-LLM image compression ported from
  the upstream engine's `image/` subsystem (techniques preserve/full_low/crop/transcode = aspect-preserving LANCZOS
  downscale + efficient re-encode), using the **REAL** SigLIP image-encoder ONNX model (~94 MB)
  as a content-similarity verifier so compression never destroys content. Verified: 1600×1200 → 768×576,
  90.6% bytes saved, SigLIP cosine ~0.997; a 512px tier cuts OpenAI vision tokens ~67%. Pillow-only
  fallback works without the model. (`[onnx]` extra now includes pillow.)
- **All four real upstream ONNX models now run inside Simplicio**: kompress-v2-base (compression),
  technique-router-onnx (routing), all-MiniLM-L6-v2-onnx (embeddings), siglip-image-encoder-onnx (image).

### Scope note — the native performance modules
The upstream's native performance modules (the core/proxy/py modules) are a **native performance re-implementation** of the
Python — its parity harness literally asserts native == Python. They add **no new capability** (just native
speed). The functional surface they cover is already in Simplicio's Python engine, so there is no
*capability* gap there — only an optional native-speed rewrite, which is out of scope for the token monitor.

## [2.10.0] — 2026-06-24

### Added — more upstream subsystems ported (3 agents; 2 more REAL upstream models)
- **`simplicio detect`** (`engine/simplicio_detect.py`, stdlib) — content-type detector (JSON/code/log/
  markdown/prose) + a **universal smart-compress** that routes each block to the best technique
  (JSON→minify, log→full pipeline, code/prose left intact). Verified 15/15: JSON 60%, log 95% saved,
  code/prose byte-preserved.
- **`simplicio router`** (`engine/simplicio_router.py`) — the **REAL** technique-router ONNX
  model (~32 MB, INT8): tokenize → ONNX → softmax → technique class (transcode/crop/preserve/full_low).
  Verified running on the real weights. (Note: this router was trained on image-edit *intents*, so raw
  text blobs tend to route to `preserve` — the model runs correctly; its training domain differs.)
- **`simplicio embed`** (`engine/simplicio_embed.py`) — the **EXACT** upstream embedder
  `Qdrant/all-MiniLM-L6-v2-onnx` (~90 MB): masked mean-pooling → 384-dim L2-normalized vectors;
  embedding RAG over the CCR store. Verified: paraphrase cosine **0.957**, unrelated −0.01, #1 rank.
- New `[onnx]` optional extra installs onnxruntime + huggingface_hub + tokenizers for `kompress`/`router`/`embed`.

## [2.9.0] — 2026-06-24

### Added — the REAL upstream ONNX compression model, integrated (the gap is closed, not substituted)
- **`simplicio kompress`** (`engine/simplicio_kompress.py`) runs the **actual upstream model**
  `kompress-v2-base` — the real ONNX semantic token-pruning model the upstream engine uses. Turns out
  its weights are **public on HuggingFace** (Apache-2.0), not proprietary: so this is the genuine
  article, not a look-alike. It tokenizes (ModernBERT), runs the ONNX session
  (`input_ids`/`attention_mask` → per-token `final_scores` keep probability), keeps the top
  `--keep` fraction of words, drops filler, and reconstructs — **reversibly** (the dropped spans are
  retained). Verified with the real model: e.g. `--keep 0.5` → 48.7% words pruned, high-signal tokens
  (identifiers, numbers, errors) preserved.
- Opt-in: `pip install "simplicio-loop[kompress]"` (onnxruntime + huggingface_hub + tokenizers; the
  ~274 MB model downloads on first use). Without it, `simplicio kompress` reports how to enable it.

### Fixed
- Engine CLI now forwards sibling-command args **verbatim** (raw passthrough) — `argparse` REMAINDER was
  mangling `--flag value` ordering (e.g. `kompress --keep 0.5` arrived as `0.5 --keep`).

### Scope — now honestly complete on the implementable + the model
With the real `kompress-v2-base` integrated, the upstream's ONNX semantic compression is no longer a
gap — it's the same model, in Simplicio. Combined with the deterministic 12-algo + extractive
compression, the model2vec embedding backend, and TF-IDF/embedding RAG, the upstream compression+RAG
surface is covered (deterministic core stdlib-only; the heavy models are optional extras).

## [2.8.0] — 2026-06-24

### Added — REAL embedding ML backend (the ML gap, done honestly — not stubbed)
- **`engine/simplicio_semantic_ml.py`** — an optional, dependency-gated embedding backend using a
  **real sentence-embedding model** (`model2vec`, static embeddings, ~30 MB, no torch):
  - **`simplicio semantic --ml`** — embedding **semantic dedup**: drops paraphrased / semantically
    redundant lines that TF-IDF + SimHash can't catch, **reversibly** (byte-exact restore). Verified
    with the real model: paraphrase cluster → 27-40% saved, round-trip OK.
  - **`simplicio rag --ml "<query>"`** — retrieval by **meaning** (embedding cosine), not keyword.
    Verified: matched a query to a lexically-disjoint memory (cosine 0.42, ranked #1) — a match
    TF-IDF would miss.
- **Opt-in + graceful**: needs `pip install "simplicio-loop[ml]"` (model2vec + numpy). Without it,
  `--ml` prints how to enable it and the system falls back to the deterministic `semantic`/`rag`.
  The native engine itself stays **stdlib-only / zero-dependency**.
- Added the `[ml]` optional-dependency extra; `--ml` routes via `parse_known_args` passthrough.

### Honest note
This uses a *real* trained embedding model (so semantic similarity genuinely works — paraphrases
match, unrelated text doesn't). It is the light static-embedding tier; a larger model catches more
paraphrase. It is NOT a reimplementation of the upstream's specific trained ONNX compression model
(that exact model isn't replicable) — but the ML *capability* (semantic compression + meaning
retrieval) is now real and verified, behind an optional dependency.

## [2.7.0] — 2026-06-24

### Added — semantic-lite compression + RAG (the honest take on the ML gap)
- **`simplicio semantic`** (`engine/simplicio_semantic.py`) — **reversible extractive** compression for
  large content: scores lines/sentences by TF-IDF + position + length, keeps the salient ones (always
  keeps headers/ERROR lines), and elides the rest with a marker — the dropped bytes are retained so
  `semantic_restore` reproduces the **byte-exact original** (lossless round-trip). Plus **SimHash**
  near-duplicate block folding, and optional CCR integration (stash the restore blob in the memory
  store, retrieve on demand). Verified: 121-line doc → 56.3% smaller, byte-exact restore.
- **`simplicio rag`** (`engine/simplicio_rag.py`) — **TF-IDF cosine retrieval** over the CCR memory
  store: `rag "<query>"` ranks stored memories by relevance with snippets; `rag remember <key> <text>`
  populates it. Verified: relevant doc ranks #1 across queries.

### Honest scope
These are **deterministic** techniques — extractive summarization + SimHash + TF-IDF retrieval — **not**
trained embedding/ONNX models. They address the "semantic compression" and "RAG" gaps with real,
zero-dependency, reversible methods; they do not do abstractive rewriting or embedding-space matching.
The trained-ONNX semantic model and embedding-vector RAG of the upstream remain out of scope (they
require ML models, not stdlib code) — and are not faked.

## [2.6.0] — 2026-06-24

### Added — output token capture (input + output now complete)
- The proxy was only counting **input** (prompt) tokens; it now also captures **output/completion**
  tokens by reading the upstream response's `usage` (OpenAI `completion_tokens` / Anthropic
  `output_tokens`) from a bounded 64 KB response tail — **without breaking streaming** (chunks are
  written through immediately; only a small tail is kept). Honest: if the upstream doesn't report
  usage, output is 0 (no fabricated estimate). Recorded as `total_output_tokens` (lifetime + session)
  and `tok_out=` in the PERF log. Verified isolated: a response with `completion_tokens:42` → captured 42.
- Dashboard shows a **tokens out** KPI (replacing the always-zero "cache hit" card — the native engine
  doesn't cache).

## [2.5.0] — 2026-06-24

### Added — more native commands + quality gates (5 more parallel agents, each self-tested)
- **`simplicio audit <paths>`** — scan files/dirs and rank how many tokens compression would save.
- **`simplicio capture --file body.json`** — dry-run analyzer: what a request would compress/save, no send.
- **`simplicio evals`** — compression eval + **regression gate** (corpus → %saved, asserts prose/code stay
  byte-identical + idempotence). Doubles as CI: exits non-zero if a change corrupts content or stops saving.
  Current gate: **4/4 invariants PASS, avg ~44% saved**.
- **`engine/simplicio_tokens.py`** — calibrated stdlib token estimator (prose ~4.1 c/tok, code ~2.9, json
  ~1.8). The proxy + capture now measure tokens with it instead of naive chars/4.
- **`engine/README.md`** — full engine reference (commands, capture mechanism, compression catalog, honest
  scope).

### Fixed
- The unified `bin/simplicio` CLI didn't route `wrap`/`report`/`verify` (and the new `audit`/`capture`/`evals`)
  — `ENGINE_CMDS` now forwards them all. README corrected accordingly.

## [2.4.0] — 2026-06-24

### Added — unified CLI + more engine commands (5 more parallel agents, each self-tested)
- **Unified `simplicio` command** (`engine/simplicio_cli.py` + `bin/simplicio`): one entry that dispatches
  `simplicio proxy|doctor|memory|mcp|init|wrap|report|verify|compress|version`.
- **`simplicio wrap <client>`** (`engine/simplicio_wrap.py`): run a client (claude/codex/cursor/opencode/aider)
  with capture routing injected for that run (OPENAI/ANTHROPIC_BASE_URL → proxy), warns if the proxy is down.
- **`simplicio report`** (`engine/simplicio_report.py`): savings report — lifetime/session totals + per-model
  and per-provider breakdown (deltas from the cumulative history), `--json`, `--since`, `--top`.
- **`simplicio verify`** (`engine/simplicio_verify.py`): one-command self-check of the whole stack
  (proxy, monitor, savings file, engine, compression, memory, MCP, operator) → PASS/WARN/FAIL table.
  Verified **8/8 PASS** on the dev machine.
- **`engine/simplicio_compress_extra.py`** — 4 more safe deterministic algorithms (markdown-table
  whitespace, repeated multi-line block fold, long-token elision, numbered-noise fold), chained after the
  base pipeline. Meaning-preserving + idempotent (prose/code byte-identical).
- `wrap`/`report`/`verify` are also reachable as `simplicio_engine` subcommands.

## [2.3.0] — 2026-06-24

### Added — native engine grows toward feature parity (built by 5 parallel agents, each self-tested)
- **`engine/simplicio_mcp.py`** — native stdio MCP server (JSON-RPC 2.0) exposing `simplicio_compress`,
  `simplicio_retrieve`, `simplicio_stats` tools. `simplicio_engine mcp` runs it.
- **`engine/simplicio_memory.py`** — CCR (compress-cache-retrieve) key-value store with byte-exact
  lossless recall (zlib+base64), atomic + thread-safe. `simplicio_engine memory remember/recall/forget/list`.
- **`engine/simplicio_compress.py`** — 8-algorithm deterministic compression (ANSI strip, trailing ws,
  blank collapse, line dedup, JSON minify, rule-run cap, hex-dump fold, fenced-log fold), idempotent and
  meaning-preserving. The proxy now uses it (verbose logs ~89-94% saved; clean prose/code untouched).
- **`engine/simplicio_init.py`** — native client integration writer (mirrors the upstream engine's `init`): registers
  the Simplicio MCP server into codex/claude/copilot/openclaw configs. **Dry-run by default**, `--apply`
  to write, idempotent. `simplicio_engine init <client>`.

### Verified
- **systemd activation field-tested on real Linux** (systemd PID 1 in Docker, aarch64): `systemctl start`
  brought the proxy up, `/health` returned `engine: simplicio`, and `Restart=always` re-spawned it after
  a kill — the previously-untested gap is now closed. `install_services.py` now sets `SIMPLICIO_HOME` on
  the services so savings/logs write even under an unset service `$HOME`.

### Fixed
- The compression module was named `compression`, which **collides with Python 3.14's new stdlib
  `compression` package** — renamed to `simplicio_compress` so the 8-algo pipeline actually loads on 3.14.

## [2.2.1] — 2026-06-24

### Verified — Linux is now field-tested (not just code-complete)
- Ran the stack inside a real Linux container (`python:3.12-slim`, py 3.12.13): the **native engine
  forwards + captures** (savings written), the **dashboard `get_status` + HTML** compose (7 runtimes),
  `install_services.py selftest` **PASS** (systemd units + Windows launchers), the **tray loads**
  (headless fallback, no crash), and the generated **systemd unit resolves the Linux Python path**.
  The honest remaining gap: systemd *daemon activation* (needs a real init host) and Windows *runtime*
  are still not exercised on those hosts — the software + artifacts are verified, service start-up is not.

## [2.2.0] — 2026-06-24

### Fixed — the installer now ships the token economy too
- **The main installer (`install_lib.py`) was disconnected from the token monitor.** It copied the
  6 skills + hooks but never set up the capture proxy / dashboard / tray, so a fresh user got the
  loop but **not** the token economy. Now the installer always prints how to enable the monitor, and
  `--with-monitor` installs the tray dep + registers the three services (`install_services.py install`).
  Verified on a fresh temp target: skills copied, hooks wired, monitor pointer shown, services
  selftest PASS.
- Removed a personal path (`~/Projetos/ai/hermes-agent/...`) from the `simplicio-engine` fallback —
  it would never resolve for other users.

## [2.1.0] — 2026-06-24

### Added
- **Live multi-runtime "active / blinking" detection.** The dashboard now detects which runtimes
  are actually RUNNING (process match) and shows them blinking: `● active` (blue) when running,
  `● capturing` (green) when their traffic is being saved in the last 10 min, `○ ready` otherwise.
  So with Claude open + Hermes on, both are recognized and blink. Header shows the active count.
- **Per-provider routing in the native engine** (`gpt→openai`, `claude→anthropic`, `deepseek→deepseek`,
  …): one capture proxy forwards each model to its REAL provider with the client's own key — captures
  every routable runtime **without swapping its model**. Verified live (gpt→OpenAI, deepseek→DeepSeek).
- **5-algorithm deterministic compression** (ANSI strip, rule-run cap, line dedup, whitespace, JSON
  minify). Verified capture coverage: OpenAI stream/non-stream, Anthropic, multimodal, concurrent,
  non-JSON fail-open — every request through the proxy is captured.

### Removed
- **Gemini, Kiro, Antigravity** dropped from the runtimes list — they use proprietary Google/AWS APIs
  the proxy can't intercept. Only the 7 genuinely-interceptable runtimes remain.

## [2.0.0] — 2026-06-24

### Added — native Simplicio capture engine (no external dependency)
- **`engine/simplicio_engine.py`** — a self-contained, stdlib-only capture proxy that **replaces the
  external compression binary** for the core capture path. It transparently forwards each request to
  the real upstream (**no model swap**), measures prompt tokens, applies **deterministic** compression
  (whitespace collapse, consecutive-line dedup, oversized-output capping), streams the response back,
  and writes `~/.simplicio/proxy_savings.json` in the same schema-v3 the Token Monitor reads. It is
  **fail-open**: any parse/compress error forwards the original bytes unchanged. Commands: `proxy`,
  `doctor`, `memory stats`, `--version`.

### Changed
- **The live capture proxy is now the native engine.** Verified end-to-end: a request through it
  reached DeepSeek's real API and returned DeepSeek's own auth error (proving transparent forwarding);
  a compressible payload was deduped 575→54 chars and recorded as real savings. Lifetime history was
  migrated from the legacy data dir → `~/.simplicio` for continuity (401,925 tokens preserved).
- `scripts/simplicio-engine` is **native-first** (falls back to an external binary only if the module
  is absent). `setup_simplicio.sh` and `install_services.py` run the native engine — `setup` no longer
  installs the external compression binary.
- README accelerator row + `token-capture.md` describe the native engine (schema-compatible with the
  upstream compression project, credited).

### Honest scope
- The native engine is the **core** (transparent capture + measurement + deterministic compression).
  It is **not** a reimplementation of the upstream engine's 360k-LOC feature set (ONNX semantic
  compression, the 6-algorithm suite, RAG, MCP memory store). Those remain out of scope; the native
  engine delivers real, safe token savings without any external dependency.

## [1.9.0] — 2026-06-24

### Added
- **Active-LLM banner** — the dashboard now detects which LLM is currently being intercepted (from the
  latest request in `proxy_savings.json` history) and shows a banner "⚡ Saving tokens for `<model>`"
  with the **LLM's logo** (DeepSeek, Anthropic, OpenAI, Gemini, Llama, Mistral, Qwen, xAI, Kimi, Groq…),
  the tokens saved, and the **last-call datetime** + relative time.
- **Datetime records throughout** — real timestamps from the capture history: last-call datetime on the
  chart, session-start datetime in the footer, full `YYYY-MM-DD HH:MM:SS` "updated" stamp, and the
  per-request `ts` carried on the series.

### Fixed
- Topbar "intercepting" chip showed `0` — now reflects `<ready>/<total>` runtimes (e.g. 7/10).

## [1.8.1] — 2026-06-24

### Changed
- **Documented the Simplicio Token Monitor in the README** (web dashboard `:9090` + menu-bar tray +
  the `simplicio-economy.sh` module + cross-platform install) so it is a discoverable, complete
  deliverable. Rebranded the token-economy table's accelerator row to "Simplicio capture proxy".
- QA pass: monitor verified fully functional — all API fields present, real-time auto-refresh
  (live token count growing), no-data/error fallbacks, 0 console errors, tray reading live data.

### Fixed (cross-platform hardening)
- **pystray tray backend verified at runtime** (not just constructed) — renders the menu-bar icon;
  added `SIMPLICIO_TRAY_BACKEND=rumps|pystray|headless` to force/test a backend.
- **Windows Startup launcher bug**: `set K=V & ...` baked a trailing space into the value; now uses
  quoted `set "K=V"` per line.
- **systemd units** get an explicit `PATH` so the engine binary resolves under systemd's minimal env.
- **Dashboard engine call is cross-platform**: invokes the binary directly on Windows (the
  `simplicio-engine` bash wrapper can't run there).
- Added `python3 scripts/install_services.py selftest` — validates the generated systemd/Windows
  artifacts on any OS (PASS on macOS).

### Honest caveats
- Verified end-to-end on **macOS** (dashboard, rumps + pystray trays, launchd, real capture). The
  **Linux systemd and Windows Startup service activation are NOT yet run on those OSes** — only their
  generated artifacts are validated. The capture engine is the third-party upstream compression binary.
  Provider interceptability (141/144) is a catalog estimate, not verified per provider.

## [1.8.0] — 2026-06-23

### Added
- **Cross-platform (macOS · Linux · Windows).** `scripts/install_services.py` registers the three
  always-on services on whichever OS you run it — launchd (macOS), systemd `--user` (Linux),
  Startup-folder launchers (Windows) — plus cross-platform `wire`/`unwire`/`status`. The tray
  (`app/simplicio_tray.py`) now auto-selects **rumps** on macOS (native menu-bar number) and
  **pystray** on Windows/Linux, with a headless print fallback.
- **Provider interceptability catalog (`app/providers.json`)** derived from the Hermes/OpenCode
  provider lists: **141 of 144 providers (98%) are interceptable** (139 OpenAI-compatible + 2
  Anthropic; only 3 Google-native are not). The dashboard surfaces the live `141/144` count next
  to the runtime panel — interception is really about providers, and we cover essentially all of them.

## [1.7.0] — 2026-06-23

### Added
- **Always-capture wiring (`simplicio-economy wire` / `unwire`).** Routes OpenAI-compatible
  clients (Codex, Cursor, OpenCode, any `OPENAI_BASE_URL` tool) through the local capture proxy —
  the **same upstream they already use, now intercepted + compressed**, with no model swap. This is
  the "works after install without invoking simplicio-loop" switch: once wired, every call is
  captured on the next shell/tool launch. Idempotent; backs up `~/.zshrc`; fully reversible via
  `unwire`. `setup_simplicio.sh` runs it so a fresh install turns capture on. `status` reports the
  wire state.

### Notes
- Activating always-capture rewrites `OPENAI_BASE_URL` in `~/.zshrc` (high blast radius across all
  OpenAI-compatible tools). That is intentional and what the install does on the user's behalf; an
  assistant running mid-session is (correctly) gated by the permission guard and must let the user
  run `simplicio-economy wire` themselves.

## [1.6.0] — 2026-06-23

### Added
- **Token-economy module (`scripts/simplicio-economy.sh`).** One entrypoint that brings up and
  reports the whole always-on savings stack — capture proxy + token monitor + menu-bar tray +
  the deterministic operator `simplicio-dev-cli` + lifetime savings — so token capture/savings
  work **after install without invoking simplicio-loop**. `setup_simplicio.sh` runs it at the end.
  Subcommands: `status`, `up`, `capture <openai|anthropic> [port]`.
- **Transparent capture proxy** (`simplicio-economy capture openai`) — forwards each call to the
  client's REAL provider, capturing tokens without swapping the model. **Verified end-to-end:** a
  real `gpt-5.4` request through the transparent proxy returned a genuine OpenAI response and the
  proxy's `/stats` recorded the request (`api_requests: 4`, `total_tokens_before: 124`). This is
  the correct path to capture Codex/Cursor/OpenCode, kept separate from the Hermes→DeepSeek proxy.

## [1.5.0] — 2026-06-23

### Added
- **Desktop menu-bar app (`app/simplicio_tray.py`).** A macOS tray + widget that lives in the
  menu bar showing live tokens saved (brand hexagon icon + compact count, e.g. `⬡ 102.9K`). The
  dropdown is the widget: lifetime tokens/$ saved, reduction %, requests, current-session savings,
  capture-proxy status, and "Open Token Monitor". Reads `proxy_savings.json` directly — no traffic
  of its own. Auto-starts as the `ai.simplicio.tray` launchd service; `setup_simplicio.sh` installs
  `rumps` and registers it.
- Brand `assets/tray-icon.png` for the menu-bar item.

## [1.4.0] — 2026-06-23

### Added
- **`scripts/simplicio-engine`** — a single Simplicio-branded wrapper around the capture engine
  binary, so the dashboard, scripts and docs speak `simplicio-engine` instead of the engine's
  own name. It is now the *only* place that resolves the underlying binary (fast lookup, no
  full-`$HOME` scan).

### Changed
- **Robust proxy detection.** The monitor now checks the proxy with a pure-Python socket connect
  instead of `lsof`, which the launchd service could not find on its restricted `PATH` (it lives
  in `/usr/sbin`) — the dashboard was falsely showing the proxy as down. Also added `/usr/sbin`
  to the generated service `PATH`s. "Always works", regardless of environment.
- Dashboard + capture script now call the engine through `simplicio-engine`; remaining upstream
  references are isolated to the wrapper's binary resolution, the engine's own data dirs
  (read-only), and the literal upstream package name.

### Notes
- **Capture activation verified.** `<engine> init <client>` was confirmed to add only a safe MCP
  integration (memory/retrieve tools) — it does NOT change a client's model or base URL. Real
  token capture requires routing a client's traffic through the proxy; with the current
  DeepSeek-pinned proxy that would swap OpenAI clients' model, so transparent multi-provider
  routing is required before activating Codex/Cursor/OpenCode (see
  `references/token-capture.md`). Claude (Anthropic format) can capture transparently.

## [1.3.0] — 2026-06-23

### Changed
- **Token Monitor is now data-forward.** Replaced the large logo hero with a compact top bar
  (small badge + green/yellow wordmark + live status chips) and gave the screen to the data:
  a **real-time token chart** (before / after / saved area) driven by the engine's request
  history, plus the savings gauge and a tighter KPI strip.
- **Primary data source is now `proxy_savings.json`** (lifetime + per-request history), with the
  raw proxy log kept as fallback — more robust and exact than log scraping, and it exposes the
  real provider/model of each intercepted request.

### Added
- **"LLMs / runtimes we intercept" panel with per-runtime logos** and an honest interceptability
  tier: `native` (engine durable integration: Claude, Codex, VS Code/Copilot, OpenClaw),
  `base-url` (OpenAI/Anthropic-compatible: Hermes, Cursor, OpenCode), `not interceptable`
  (proprietary APIs: Gemini, Kiro, Antigravity). Shows 7/10 interceptable, dimming the rest.
- **$ saved** KPI and a models-intercepted readout sourced from real request history.

## [1.2.0] — 2026-06-23

### Changed
- **Rebranded the token monitor from the upstream branding to Simplicio.** The localhost dashboard is now
  the **Simplicio Token Monitor** (header + footer brand lockup rendered green + yellow).
  Our hooks/services/files were renamed to the `simplicio_*` scheme: the dashboard hook, the watch
  hook, and the setup script became `hooks/simplicio_dashboard.py`, `hooks/simplicio_watch.py`,
  `scripts/setup_simplicio.sh`; launchd services were renamed to `ai.simplicio.proxy`
  and `ai.simplicio.token-monitor`; the proxy-port env var → `SIMPLICIO_PROXY_PORT`
  (old name still honored as fallback); proxy log
  targets → `~/.hermes/logs/simplicio-proxy*.log`.
- **Carve-out:** the underlying compression accelerator is the third-party upstream
  product, so its real binary/install names are kept functional (its `pip install`,
  its `proxy` and `memory stats` commands) and its OSS attribution is preserved — only
  Simplicio-owned naming was changed.

### Added
- **Token Monitor auto-starts on macOS** via the renamed launchd service `ai.simplicio.token-monitor`,
  so the dashboard is live without a manual start.

## [1.1.0] — 2026-06-23

### Changed
- **Token dashboard redesigned to the Simplicio brand.** The localhost monitor
  (`hooks/simplicio_dashboard.py`, `:9090`) now renders the full Simplicio lockup faithfully
  in a neon-framed hero instead of a cropped square, echoes the brand tagline as four pillars
  (smart orchestration · neural cache · compressed context · maximum efficiency), and leads
  with a savings gauge (reduction %) + before→after token flow.
- **Runtime coverage is now a first-class panel** — all ten supported runtimes (Claude, Codex,
  Hermes, OpenClaw, VS Code, Gemini, Cursor, OpenCode, Kiro, Antigravity) each show how the
  skills load and how the loop drive is bound, with a coverage state pill.
- **Front-end construction cleaned up.** The single opaque HTML blob is split into
  `STYLE` / `BADGE_SVG` / `BODY` / `SCRIPT` constants composed via placeholder substitution —
  still single-file (deploy-friendly), no longer one unmaintainable string. The backend
  (`get_status` + handlers) is unchanged.

### Added
- **Repo-local brand asset** `assets/simplicio-loop-logo.png` — the dashboard now serves the
  logo from inside the repo (first logo candidate) instead of depending on a path outside it.
- **Faithful inline badge** (`BADGE_SVG`) — a vector of the hexagon-S mark (extruded S +
  stacked-layers core + circuit traces + speed particles), used as the favicon and the no-PNG
  fallback logo.

### Fixed
- Log viewer `tok_*=` highlight (`.hl`) had no CSS rule and rendered unstyled — now themed green.

## [1.0.5] — 2026-06-23

- Upstream compression integration: live web dashboard + monitor on `:9090`, context-compression proxy,
  MCP accelerator, setup script and launchd services.
- LMCache inference accelerator, agentsview session-observability source adapter.
- 11 runtime adapters + universal installer; hardened Ralph loop with bound operators
  (`simplicio-mapper` + `simplicio-cli`).
