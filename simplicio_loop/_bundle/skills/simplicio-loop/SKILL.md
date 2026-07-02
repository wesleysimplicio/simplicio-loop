---
name: simplicio-loop
description: "Iterate on a task autonomously until a typed completion-promise is genuinely true or a max-iteration cap is hit — the Ralph Wiggum loop, hardened. Use when the user says \"ralph loop\", \"keep iterating until done\", \"loop on this until it passes\", or when simplicio-tasks needs a self-referential drive that re-feeds the same goal each turn and sees its own prior work. Runtime-agnostic: binds a real stop-hook where the host supports hooks (Claude, Cursor); otherwise self-paces via the host scheduler. Never escapes the loop with a false promise."
---

# simplicio-loop — the hardened Ralph loop

A self-referential iteration primitive: the SAME goal is fed back after every turn, so
the agent sees its own prior edits and converges. It exits ONLY when a **typed
completion-promise** is genuinely true, or a hard `max_iterations` cap fires. This is the
drive underneath `simplicio-tasks`' 24/7 watcher (Step 3b/7) extracted as a reusable,
inspectable, cancellable skill.

Credit: the technique is Ralph Wiggum / cursor `ralph-loop`. We keep its best parts —
single human-readable state file, exact-match promise sentinel, two-hook split — and add
the simplicio safety spine (evidence-gated promise, budget kill-switch, cross-platform hook).

## Normative contract (non-negotiable)

These invariants are MUST-level. Any runtime that loads this skill (Hermes, Claude, Cursor, or a
bare LLM) follows them mechanically — no paraphrase, no drift:

1. **Evidence-gated exit.** The loop MUST NOT terminate without concrete evidence, produced in the
   SAME turn, that the goal is met. No in-turn evidence → no exit.
2. **Exact promise.** Completion is gated by the EXACT sentinel `<promise>EXACT TEXT</promise>`
   equal to `completion_promise` verbatim. A paraphrase or a fuzzy "I'm done" never counts.
3. **Deterministic continuation.** If the promise is not satisfied, the next iteration MUST re-feed
   the current goal + state unchanged — a mechanical re-feed, never a manual "shall I continue?".
4. **Bounded by construction.** `max_iterations` OR a budget ceiling MUST be set before iteration 1
   — the loop is NEVER unbounded — and the cap/budget is checked BEFORE every continuation.
5. **Single source of truth.** All loop state lives in the one scratchpad below; the sibling
   `.orchestrator/loop/done` flag is touched ONLY when the promise is verified.
6. **Fallback obeys the same contract.** When the host has no hooks, the self-paced scheduler mode
   is first-class and MUST honor invariants 1–5 identically.

The rest of this file is the mechanism that enforces this contract.

## When to use

- "run a ralph loop on X", "iterate until the tests pass", "keep going until done".
- As the engine for `simplicio-tasks` when it must drain a queue unattended.
- NOT for a one-shot edit — use the host's normal flow.

## Bound operators (REQUIRED): survey + operate

This loop does NOT survey the repo with the LLM, and it does NOT hand-edit files with the LLM.
Two installed CLIs are the operators; the model only DECIDES, the operators DO. Both ship as
hard dependencies of the `simplicio-loop` package (`pip install simplicio-loop` pulls them):

| Operator | CLI (binary) | Binds | Role in the loop |
|---|---|---|---|
| **simplicio-mapper** | `simplicio-mapper` | `orient` / `recall` | **Survey** — maps the repo(s) into `.simplicio/*.json` (project-map, precedent-index, symbol-index, call-graph, docs). Two-tier (v0.9+): `macro` is an instant shallow skeleton (no content reads), `scan` returns that skeleton now and runs the deep index in the background, `status` reports the deep-pass phase. v0.13+ adds `inspect` (machine-readable evidence that the artifacts actually exist — the survey's own evidence gate) and `handoff` (a compact context-pack — files, symbols, deps, `pack_hash` — that feeds the goal instead of re-reading the tree). This survey, not an ad-hoc LLM read, is what feeds the goal each turn. |
| **simplicio-dev-cli** | `simplicio-dev-cli` | `execute` / `deterministic_edit` / `validate` / `diagnostics` | **Operate** — applies a DECIDED change through its 6-layer contract (mapper context → precedent → prompt → diff → test → verify, ≤3 retries). The CLI edits and verifies; the AI does not hand-write the diff. |

**Preflight (MANDATORY, BLOCKING).** Before iteration 1, auto-update both operators to their latest
release (so every run uses the newest `simplicio-mapper`/`simplicio-cli`), then confirm both are on
PATH:
```bash
# Always run the loop on the latest operators. FAIL-OPEN: offline / no-pip / a pin keeps the
# currently-installed build; this never blocks. Runs ONCE per loop preflight, not per turn.
python3 -m pip install -qU simplicio-mapper simplicio-cli 2>/dev/null \
  || python3 -m pip install -qU --user --break-system-packages simplicio-mapper simplicio-cli 2>/dev/null || true
simplicio-mapper --version   # survey operator (now latest)
simplicio-dev-cli --help     # action operator (pkg simplicio-cli; exposes `simplicio-dev-cli`)
```
The auto-update is best-effort and offline-safe — a network/pip failure leaves the working version
in place and the loop proceeds. The action binary is `simplicio-dev-cli` (from `pip install simplicio-cli`) — NOT the bare
`simplicio`, which is reserved for the separate `simplicio-runtime` and is not what this loop
binds. `simplicio-dev-cli` has no `--version` subcommand; `--help` exiting 0 is the readiness
proof. If either operator is missing, do NOT fall back to LLM survey/editing — STOP and emit
`simplicio-loop: BLOCKED — missing operator <name>; run: pip install simplicio-loop` (the install
re-pulls `simplicio-mapper` + `simplicio-cli`). This requirement is scoped to the loop drive.

**Survey step (each loop start + on any structural change).** Prefer the two-tier flow (v0.9+):
`simplicio-mapper scan . --json` returns an instant `macro` skeleton AND kicks the deep index off in
the background — the loop starts working immediately instead of blocking on a full crawl. Poll
`simplicio-mapper status . --json` (`phase`: `deep_running` → terminal) before relying on the deep
artifacts; pass `--await [--timeout <s>]` to block until terminal, or `scan --sync` (forced when
`CI=true`) for the old single-shot behavior. `simplicio-mapper index . --json` (add `--watch` for
long runs) remains the synchronous full (re)build of `.simplicio/`. Read the survey artifacts —
never re-scan the tree by hand when a fresh map exists. For a multi-repo survey, run the mapper per
repo root and aggregate the JSON.

**Survey evidence gate + context-pack (v0.13+).** Before trusting the deep artifacts, gate on
`simplicio-mapper inspect . --json [--await]` (`simplicio.map-inspection/v1`): it reports, per
artifact (project-map, precedent-index, symbol-index, call-graph, index-state, map-job,
context-cache), whether the file **exists on disk** with size + mtime, plus `warnings`. An artifact
the inspection says is missing must be treated as absent — re-run `scan`/`index`, don't guess its
content. This is the same evidence-not-claims discipline the promise gate applies, applied to the
survey itself. Then feed the goal from `simplicio-mapper handoff . --json [--await]`
(`simplicio.map-handoff/v1`): its `context_pack` carries the relevant files with symbols, imports,
dependencies, `recent_changes` and a `pack_hash` — a pre-compressed orientation bundle that
substitutes for re-reading the tree (token economy: pack first, raw `Read` only for the few files
the pack points at). Honor `context_pack.llm_directives` (no-think / no-internet / minimal tools)
for the mechanical steps, and use `needs_broader_context` as the signal that the pack alone is not
enough. If the installed mapper predates 0.13 (`inspect`/`handoff` absent from `--help`), the
preflight auto-update already pulls a current build; offline, fall back to `status` + reading
`.simplicio/*.json` directly — the gate is then the file-existence check you do by hand.

**Operate step (every turn that mutates code).** Once the AC and the change are DECIDED, delegate
the mutation to the operator, one decided change at a time:
```bash
simplicio-dev-cli task "<the decided, AC-scoped change>" --target <file> [--json]
```
The operator applies the diff, runs the tests, and self-corrects up to 3× — its passing
verification IS the in-turn evidence the promise gate needs (below). The AI never edits the file
directly inside the loop; if `simplicio-dev-cli` cannot complete a change after its retries, treat that
as a genuine blocker to investigate, not a reason to hand-edit around it.

**Where each operator fires.** The AI only DECIDES (triage, AC extraction, choosing the change,
merge/close gates); the operators do survey + apply:

| Phase | Operator | Command |
|---|---|---|
| Preflight (before iteration 1) | both | `python3 -m pip install -qU simplicio-mapper simplicio-cli` (auto-update to latest, fail-open) → `simplicio-mapper --version` · `simplicio-dev-cli --help` → BLOCK if missing |
| Survey (loop start; multi-repo: per root) | mapper | `simplicio-mapper scan . --json` (instant macro + deep index in background; `--sync`/`--await` to block) → `.simplicio/*.json`. `index . --json` for a forced synchronous build. Gate: `inspect . --json` (artifacts exist on disk) → feed goal: `handoff . --json` (context-pack) |
| Loop contract step 2 — Triage (every turn) | mapper | `simplicio-mapper handoff . --json` → work from the `context_pack` (symbols/deps/recent_changes); `macro . --json` for an instant skeleton, or `scan`/`status` + `inspect` to refresh/re-gate if the tree changed |
| Loop contract step 3 — Work the goal | dev-cli | `simplicio-dev-cli task "<decided change>" --target <file> [--json]` |
| Evidence-gated `<promise>` / `simplicio-tasks` Step 4b | dev-cli | the operator's passing test+verify pass = in-turn evidence |

One turn: `preflight → survey (mapper) → triage (re-read survey) → DECIDE (AI) → operate
(simplicio-dev-cli task: apply+test+retry ≤3×) → watcher-gate (independent re-execution) → <promise> only if all gates passed`.

## Video evidence producer (hyperframes) — demo videos as proof

The loop can be asked to **create a demonstration video** of a screen/feature — e.g.
`/simplicio-tasks make a demo video of the login screen` — and it uses that video as
in-turn evidence that the change works. The producer is **hyperframes**
(<https://github.com/heygen-com/hyperframes>): it renders HTML/CSS/media compositions to a
**deterministic MP4** ("same input, same frames, same output"), so the video is a CI-reproducible
artifact, not a one-off recording. No API keys; local render via headless Chrome + FFmpeg.

This is NOT a bound operator (it never BLOCKS the loop): it fires only when a turn's goal is a
video request, or when a UI change wants a moving proof. The runnable worker is
`scripts/video_evidence.py`; the full contract is `references/video-evidence.md`. One turn:

```bash
# 1. is this turn a video request?  (terminal intent gate, not the LLM)
python3 scripts/video_evidence.py detect --goal "<the re-fed goal body>"
# 2. capture the real screen (reuse web_verify — drives the UI, writes per-step PNGs)
python3 scripts/web_verify.py run --url <URL> --expect "<text>" --issue <N>
# 3. assemble those PNGs into a deterministic MP4 and attach it to the PR
python3 scripts/video_evidence.py verify --name <slug> --frames .orchestrator/tee/web \
    --title "<screen>" --issue <N> [--upload --pr <N>]
```

The MP4 path + the `video_evidence: PASS …` ledger row is the in-turn evidence the promise gate
needs; a missing toolchain (Node 22+, FFmpeg, hyperframes) yields **BLOCKED**, never a fake pass —
so a video that never rendered can never satisfy the promise.

## State file (single source of truth)

`.orchestrator/loop/scratchpad.md` — human-readable, trivially editable/cancellable:

```markdown
---
iteration: 1
max_iterations: <N or 0>          # 0 = unlimited (pair with a budget ceiling, never alone)
completion_promise: "<EXACT TEXT>" | null
evidence_required: true           # promise is rejected unless backed by a passing gate
mode: converge | drain            # which termination logic applies (see Two loop modes)
started_at: "<ISO-8601>"
---

<the task goal, verbatim — this body is re-fed every turn>
```

A sibling flag file `.orchestrator/loop/done` is `touch`ed only when the promise is verified.

Alongside it, `.orchestrator/loop/journal.jsonl` is the loop's **durable attempt memory** (one
append-only record per turn: `iteration`, `action`, `hypothesis`, `gate`, failure `fingerprint`).
The scratchpad holds the GOAL; the journal holds WHAT WAS TRIED — see § Run-journal + stall
detector below. It is the difference between a loop that converges and one that oscillates.

## The loop contract

1. **Write the scratchpad** with the goal, the cap, and the promise text. Always recommend a
   `max_iterations` safety net even when the user wants "unlimited" — pair unlimited with the
   `.orchestrator/loop-budget.json` $ kill-switch (see `simplicio-tasks` Step 1a/7).
2. **Triage the live state FIRST (mandatory).** Before any action each turn, re-read the ground
   truth — the **`simplicio-mapper` survey** (`.simplicio/*.json`; refresh it with
   `simplicio-mapper macro . --json` for an instant skeleton or `scan . --json` if the tree changed),
   `git status`/`git diff`, the working
   tree, the scratchpad notes, AND the source of record (re-query the open issues/PRs, existing
   branches, the `.orchestrator/loop/done` flag). **Also read the attempt memory FIRST**:
   `python3 scripts/loop_journal.py resume` — it lists what was already tried and the dead-end
   actions to AVOID, so the turn never re-runs a known-failing approach. For **incremental triage**
   (don't re-scan the whole tree every turn), `loop_journal.py since` shows only the delta since the
   last recorded turn's commit. **And re-read the task anchor** — `python3 scripts/task_anchor.py
   check --goal "<the goal worked this turn>" --exit-code` — so the turn stays on the SAME frozen
   acceptance criteria and cannot drift: a `DRIFT` verdict (exit 11) means the goal moved; STOP and
   re-anchor explicitly (`--force`), never wander silently. Before deciding the next code change,
   refresh the local impact map for the planned seed files with
   `python3 scripts/impact_audit.py audit <root> --file <seed> --cover <known-reviewed-file> --json
   > .orchestrator/impact-audit.json` so the turn sees callers, neighboring dependencies, and
   related tests before it edits. For shared/public contracts or signature changes, tighten that gate
   to `--fail-on medium`. For mixed front/back/service workspaces or any cross-surface user flow,
   also refresh the flow map with
   `python3 scripts/flow_audit.py audit <root> --fail-on high --json > .orchestrator/flow-audit.json`
   so triage sees UI actions, frontend calls, backend endpoints, and service calls before deciding
   the next move. The journal is the loop's memory for ATTEMPTS; the anchor is its memory for SCOPE;
   the impact audit is its memory for BLAST RADIUS; the flow audit is its memory for INTEGRATION.
   Act only on what is still genuinely open; never redo done work or act on a stale picture
   (idempotency).
3. **Work the goal** each turn as if fresh, against that triaged state. The model DECIDES the
   AC-scoped change; the **`simplicio-dev-cli` operator APPLIES and verifies it**
   (`simplicio-dev-cli task "<change>" --target <file>`) — do not hand-edit inside the loop. End EVERY
   iteration with a short, concrete verification — the operator's passing test run, or one gate /
   command / `file:line` receipt. **After the operator passes, the watcher-gate re-runs
   independently** — a separate agent/PID re-executes the work and writes
   `.orchestrator/loop/watcher_state.json` with `{"match": true, "status": "MEASURED"}` only when
   `reported == watcher.recomputed_truth`. A `match: false` or missing watcher state is treated as
   `UNVERIFIED` and gates the promise. If the actual edit surface expands, rerun `impact_audit.py` with
   the new seeds/cover and treat uncovered reverse dependents as failed verification; use
   `--fail-on medium` for shared/public contracts or signature changes. If the change crosses
   UI/API/service boundaries, rerun
   `flow_audit.py` after the edit and treat high gaps as failed verification; use `--fail-on medium`
   when the AC promises backend integration for that UI flow. **Then RECORD the attempt** in the journal:
   `loop_journal.py record --iteration N --action "<what you changed>" --hypothesis "<why>"
   --gate pass|fail --gate-output <test.log>` — on a failure the gate output is fingerprinted so the
   SAME failure is recognised next turn. Keep iterations small and verifiable: a turn that only
   edits without verifying is incomplete.
4. **Re-feed** happens at turn end via the stop-hook (below). Each re-fed turn is prefixed
   `[simplicio-loop iteration N. To finish: output <promise>TEXT</promise> ONLY when genuinely true.]`.
   Before re-feeding, the stop-hook (or the self-paced tick) runs the **stall check**
   (`loop_journal.py stall`): if the loop is STALLED, it does NOT blindly re-feed the same goal —
   it switches strategy or escalates (§ Run-journal + stall detector).
5. **Exit** by emitting the sentinel `<promise>EXACT TEXT</promise>` — and ONLY when every
   acceptance criterion is met AND a real gate passed **in the SAME turn** (`evidence_required`)
   AND the **watcher-gate** confirms the result (`watcher_state.json` with `match: true` /
   `status: MEASURED`). The watcher re-executes the work independently before the promise is
   honored — corrective gate per Asolaria N-Nest pattern.

## Two loop modes (different jobs, different termination)

A loop drains a queue and a loop converges a hard task — opposite dynamics, so the scratchpad
`mode` selects which termination logic the driver uses. Pick it when arming; default `converge`
for a single goal, `drain` for a work-queue.

| | `converge` (single hard task) | `drain` (a queue of items) |
|---|---|---|
| Wants | depth — keep changing strategy until ONE thing passes | breadth — clear many independent items, idempotently |
| Each turn | triage `since` last turn (incremental) → one AC-scoped change → verify → watcher-gate → journal | claim next open item → implement → deliver → re-query source |
| **Termination** | the evidence-gated `<promise>` fires, OR the **stall detector** says STALLED and escalates (below) | the source re-query returns empty for **K consecutive rounds** (`dry≥2`) AND the working set is idle |
| Anti-pattern it avoids | oscillation (retrying the same dead-end) | missing late-arriving work (stops too early) |

Both still obey the universal exits (promise+evidence, `max_iterations`, budget, STOP). The split
only changes WHEN "naturally done" is declared: `converge` is done when the one task is proven or
genuinely stuck; `drain` is done when the queue stays empty across rounds. Don't apply `drain`'s
"empty K times → done" to a single task (it would quit the moment a turn makes no visible change),
and don't apply `converge`'s stall-escalation to a queue (a stuck item should be quarantined, not
halt the whole drain). `simplicio-tasks` Step 3 routes fast-path/heavy-path on top of this.

## HRM-style hierarchical planner (two-level reasoning loop)

Inspired by the **Hierarchical Reasoning Model** (arXiv:2506.21734, JesseBrown1980/HRM),
the loop now operates at TWO levels instead of one:

| Level | Speed | Runs | Job |
|-------|-------|------|-----|
| **High-level planner** (`scripts/hierarchical_planner.py`) | Slow (every N turns or on stall) | `plan` subcommand called by `loop_stop.py` before each re-feed | Re-assess abstract strategy; MAY write a new **phase** (`.orchestrator/loop/phase.json`) that changes direction |
| **Low-level executor** (the loop itself) | Fast (every turn) | The normal Ralph re-feed within the current phase | Execute one AC-scoped change, verify, record to journal — never change the phase |

**Phase states** (ordered escalation):

| Phase | When | Strategy | Tactical guard |
|-------|------|----------|----------------|
| `explore` | First stall, or fresh complex bug | Survey codebase, read logs — DO NOT mutate | No edits — only read/grep/log analysis |
| `debug` | After explore, or known bug | Add instrumentation, narrow failure, prove root cause | Do not fix yet — isolate first |
| `harden` | Working code that needs safety | Add tests, edge cases, error handling | Do not add features — only safety nets |
| `refactor` | Code quality debt | Restructure without changing behavior | Zero behavior change — tests pass before AND after |
| `implement` | Default / fresh goal | Write new code against frozen ACs | One AC at a time, verify each |
| `escalate` | Deep stall (>K identical failures) | STOP mutations — gather context for human handoff | Zero mutations — only HANDOFF.md |

The planner is **deterministic and model-free** — same rules apply regardless of
LLM provider. State lives in `.orchestrator/loop/phase.json`. The loop runs in
flat mode if the planner script is missing.

**Usage:**
```bash
# Before deciding the next action each turn, read the current phase
python3 scripts/hierarchical_planner.py status
# → MEASURED|phase: debug — started at iter 3, strategy: "Add instrumentation..."

# Force replan manually (normally automatic)
python3 scripts/hierarchical_planner.py plan

# Reset to flat mode
python3 scripts/hierarchical_planner.py clear
```

## Cross-agent persistent wiki (`.orchestrator/wiki/`)

Evolved from the one-shot `HANDOFF.md` pattern (inspired by JesseBrown1980/ai-memory).
Every turn's key decisions, findings, and dead-ends are captured into a persistent
markdown wiki at `.orchestrator/wiki/` — a per-project, cross-agent, zero-friction
knowledge base that survives across agent vendors (Hermes → Claude Code → Codex).

A fresh agent arriving in the repo reads the wiki and sees "where we left off"
without needing the prior conversation transcript.

**Structure:**
```
.orchestrator/wiki/
  SUMMARY.md          — regenerated each turn; full index of all entries
  journal/            — per-turn captures (YYYY-MM-DD_HH-MM-SS.md)
  decisions/          — accepted ACs, rejected approaches, settled facts
  artifacts/          — links to evidence files, PRs, run IDs
```

**Commands:**
```bash
python3 scripts/cross_agent_wiki.py capture    # capture this turn's state
python3 scripts/cross_agent_wiki.py summary    # regenerate SUMMARY.md
python3 scripts/cross_agent_wiki.py handoff    # write HANDOFF.md for next agent
python3 scripts/cross_agent_wiki.py status     # show wiki stats
```

**How it works per turn:**
1. After each iteration, `cross_agent_wiki.py capture` saves the turn: goal, phase,
   journal stats, recent git log, working tree diff, last action + gate + fingerprint.
2. `cross_agent_wiki.py summary` regenerates the index, showing all entries with
   pass/fail counts, unique fingerprints, and distinct actions tried.
3. On handoff (cap/budget/STOP), `cross_agent_wiki.py handoff` writes a structured
   markdown with frozen goal, AC status, last 3 distinct actions (anti-oscillation),
   and explicit resume instructions for the next agent.

The wiki is plain markdown — `grep`-able by any agent, editable by any editor,
backup-able with `rsync`. No vector DB, no `write_note` ceremony.

## Run-journal + stall detector (the loop's working memory)

A re-feed loop with no memory of its own attempts has two failure modes the classic Ralph loop
cannot see: it **re-derives the same triage every turn** (wasted tokens) and it **oscillates** —
tries X, fails, tries X again — until the cap burns. The journal + stall detector close both. Both
are deterministic and model-free (`scripts/loop_journal.py`), so a resume is reproducible from disk.

**1. The run-journal — `.orchestrator/loop/journal.jsonl` (append-only attempt memory).** One
record per turn: `{iteration, action, hypothesis, gate: pass|fail|blocked, fingerprint, ts}` with
optional lineage fields such as `execution_state`, `stage_id`, `source_artifact`, `chunk_id`,
`validator`, `decision`, `retry_count`, `blocked_reason`, and `next_action`. On a failing gate the
gate output is reduced to a **stable fingerprint** — line numbers, file paths, hex/uuids,
timestamps and durations are normalized away, so the SAME bug hashes the SAME across turns even
when the incidental text differs. This is the loop's memory of WHAT WAS TRIED; the scratchpad only
holds the goal.

**2. The stall detector — `loop_journal.py stall`.** Reads the journal and returns
`PROGRESS | STALLED`. STALLED = the last **K** consecutive attempts all failed with the **same
fingerprint** (default K=3). A different fingerprint each turn = the loop is moving (PROGRESS); the
same one K times = it is spinning. On STALLED it names the **dead-end actions** (already tried under
this fingerprint) and recommends `switch-strategy` (K) or `escalate` (>K) — and `--exit-code` exits
10 for hook/`if:` gating.

**How the loop uses it each turn:**
```bash
# triage (step 2) — START here so you never retry a known dead-end
python3 scripts/loop_journal.py resume
#   → distinct actions tried + their outcomes + "AVOID (dead-ends): …" + live fingerprint
# … decide + operate + verify (step 3) …
python3 scripts/loop_journal.py record --iteration N --action "<change>" \
    --hypothesis "<why>" --gate pass|fail --gate-output <test.log> \
    --execution-state planned --stage-id validate --validator pytest --decision retry
# re-feed gate (step 4) — before re-feeding the same goal
python3 scripts/loop_journal.py stall --k 3 --exit-code
#   PROGRESS → re-feed normally
#   STALLED  → do NOT re-feed the same goal into the same failure:
#              switch strategy (change the approach, not just retry), or
#              escalate to the human_gate with the fingerprint + dead-ends, or
#              (headless, no approver) stop with a blocked status — never burn the cap spinning
```

This upgrades invariant 3 (Deterministic continuation): the next iteration re-feeds the goal **and
the attempt memory** — and a STALLED loop changes course instead of repeating itself. It also makes
resume real: a fresh process reads the journal and continues without re-deriving prior turns.

## The promise is evidence-gated (the simplicio hardening) + watcher-gate (pre-promise)

The classic Ralph loop trusts the model to be honest. We do not. A `<promise>` is accepted
only if, in the SAME turn, there is concrete evidence the work is truly done, AND the
**watcher-gate** has independently verified the result:

- the **watcher-gate** itself (Asolaria N-Nest Corrective Gate) — a separate agent/PID
  re-executes the work and compares results; `.orchestrator/loop/watcher_state.json` is written
  with `{"match": true, "status": "MEASURED"}` only when `reported == watcher.recomputed_truth`,
  or
- the run-verification gate passed ("works, not just compiles" — `simplicio-tasks` Step 4b) —
  the `simplicio-dev-cli` operator's passing test+verify pass (its contract step 5/6) satisfies this, or
- the flow coverage gate passed for a mixed front/back/service change —
  `python3 scripts/flow_audit.py audit <root> --fail-on high` (or `--fail-on medium` for ACs that
  promise backend integration) found no unhandled UI/API/service gaps, or
- the scope/impact gate passed for the changed shared files —
  `python3 scripts/impact_audit.py audit <root> --file <seed> ...` found no uncovered reverse
  dependents (and, for shared/public contracts, no uncovered local deps/tests under `--fail-on medium`), or
- the named acceptance criteria are each checked with a `file:line` or command-output receipt —
  mechanically enforced by the task anchor: `python3 scripts/task_anchor.py gate --exit-code` must
  return READY (every anchored AC `done` with a receipt; exit 12 = still pending) before the promise
  is allowed. An anchor with pending criteria makes the `<promise>` a contract violation, exactly
  like missing evidence, or
- for a queue, the source re-query confirms the items are actually closed/merged, or
- a **demo video** of the change running on screen — a deterministic MP4 rendered with
  **hyperframes** via the `video_evidence` producer (below) — whose ledger row + MP4 path prove
  the feature works end-to-end. This is the strongest "works, not just compiles" receipt for a UI
  change, and is the REQUIRED evidence when the goal was itself "make a demo video of screen X".

A `<promise>` with no evidence in-turn — OR with a failing watcher-gate — is a **contract
violation**: the capture hook ignores it (does not raise `done`) and the loop continues.
**Never output a false promise to escape the loop.** This wires the loop directly into the
repo's hard rule: *never close work without a merged PR or concrete evidence.*

**Closing is evidence-gated too (no false positives).** Declaring an item done — or closing an
issue — requires BOTH a live source re-query (the item is actually still open right now) AND
concrete evidence in the code or a linked/merged PR. A self-reported "done" with no live state
and no artifact is a false positive and is rejected, exactly like a bare promise.

## Claims-gate discipline — MEASURED/UNVERIFIED tagging

Every claim the loop makes — in the journal, in triage, in the exit promise, or in any
turn output — MUST be tagged with its evidence class. This is the Asolaria claims-gate
discipline, absorbed into simplicio-loop so no output escapes without a truth-class label.

**Two tags, no exceptions:**

| Tag | Meaning | When to use |
|-----|---------|-------------|
| `MEASURED\|` | The claim is backed by in-turn, concrete, non-model evidence | A passing gate, a `file:line` receipt, a `diff --stat`, a test log, a live API response, or any artifact the loop itself did NOT hallucinate |
| `UNVERIFIED\|` | The claim is an inference, a plan, a hypothesis, or a best-effort summary the model makes without mechanical proof | Triage notes, hypotheses in the journal, proposed next actions, stall analysis, or any claim the loop cannot prove this turn |

**Every `loop_journal.py` output is tagged.** The `record` command tags passing gates
`MEASURED\|` and failing/blocked ones `UNVERIFIED\|`. `resume` and `status` prefix every
summary line. The stall verdict is `MEASURED\|` when it reports concrete fingerprint matches,
`UNVERIFIED\|` when it recommends a next action.

**The eight rules** (from Asolaria's claims-gate contract) enforce this mechanically:

| # | Rule | Meaning |
|---|------|---------|
| 1 | **ground impact before severity** | Tag the impact (what actually broke/failed) first; the severity label follows only if measurable. |
| 2 | **no flat tuples** | Never output a bare `(MEASURED\|..., UNVERIFIED\|...)` tuple without a sentence explaining each. |
| 3 | **mirrors != authority** | A mirror/duplicate of a source is UNVERIFIED unless the loop independently checks the source. |
| 4 | **cylinders ≠ levels** | A numeric or categorical tag (iteration N, severity X) is not a claims-gate tag — always add `MEASURED\|` or `UNVERIFIED\|` explicitly. |
| 5 | **owning gate, not transcript** | The loop owns its claims-gate tags — it does NOT copy tags from transcript or tool output; it RE-tags every claim with its own assessment. |
| 6 | **missing ≠ clean-zero** | Absence of evidence is not evidence of absence — tag unresolved signals as `UNVERIFIED\|`, never skip the tag because nothing failed. |
| 7 | **real lane** | Tag every claim in the output lane the user sees (scratchpad, journal, triage, promise), not just internal debug lines. |
| 8 | **source ≠ live** | A source reference (e.g., a linked file on disk) is UNVERIFIED until the loop re-reads it this turn; a cached source is never `MEASURED\|`. |

**How to apply each turn:**

```
# triage output — hypothesis, not proof
UNVERIFIED| root cause is likely a race in the connection pool

# journal record on a passing gate
MEASURED| py_test --gate pass --fingerprint - (all 47 tests green)

# journal record on a failing gate
UNVERIFIED| integration/gate fail -- fingerprint a3b2c1 -- retry with longer timeout

# stall verdict
MEASURED| STALLED -- 3 identical fingerprints, dead-end actions: ["retry fetch"]

# exit promise
MEASURED| <promise>All acceptance criteria met</promise> -- verified by test run, flow audit, and task anchor gate

# watcher-gate (pre-promise verification)
MEASURED| watcher_state.json match:true -- agent PID result == watcher PID recomputed truth
UNVERIFIED| watcher_state.json missing or match:false -- watcher disagrees, promise rejected
```

**The eight-rule checklist is appended to every loop initialization and every triage step**
(see § The loop contract step 2): review every output claim against rules 1–8 before
proceeding. The `loop_journal.py claims-gate --check` helper audits any output blob for
untagged claims.

## Binding the hook (deterministic, near-zero token)

Where the host runtime supports lifecycle hooks, bind the two cross-platform hooks shipped in
`hooks/` (Python, so they run identically on Windows/macOS/Linux — see `hooks/hooks.json`):

| Hook | Fires | Job |
|---|---|---|
| `afterAgentResponse` → `loop_capture.py` | after every turn | extract `<promise>…</promise>`; if it exactly equals `completion_promise` AND in-turn evidence exists → `touch .orchestrator/loop/done`. Fire-and-forget, `exit 0`. Never stops the loop itself. |
| `stop` → `loop_stop.py` | when the turn ends | guard clauses, each ends the loop cleanly (remove state, `exit 0`): (1) no scratchpad → stop; (2) corrupt frontmatter → stop; (3) `done` flag present → stop (promise fulfilled); (4) `iteration >= max_iterations > 0` → write `HANDOFF.md`, then stop (cap); (5) budget halted → write `HANDOFF.md` (frozen goal + AC status + last attempts) for a different agent to resume, then stop; (6) **spindle handoff latched** → write `HANDOFF.md` and stop (the next agent will pick up); **before promise check: runs watcher-gate** — reads `.orchestrator/loop/watcher_state.json` and rejects the promise if `match: false` or `status: UNVERIFIED`; the re-feed header is tagged with `MEASURED`/`UNVERIFIED` accordingly; else increment `iteration` in place and emit `{"followup_message": "<header>\\n\\n<goal body>"}` to re-feed. |

Detection (`capture`) and termination (`stop`) are split on purpose — neither parses the
other's inline state. Iteration carries forward through git history + the working tree, not
context stuffing, so token cost per cycle stays flat.

## Self-paced drive (no hooks — a first-class path)

Hooks are an optimization, not a requirement: the self-paced drive is a primary way to run this
loop, equal in standing to the hook-bound one. When the host has no hook layer — or hook delivery
is not guaranteed — self-pace the loop with the host scheduler, exactly the `simplicio-tasks`
watcher mechanism (Step 3b "Arming the watcher"). Default to self-pacing whenever hook delivery is
uncertain rather than assuming a hook will re-feed the goal:

- Host-native durable scheduler / OS cron / a session `/loop` re-invoking this skill.
- Each tick: read scratchpad → do one iteration → check the promise+evidence → if true,
  delete state and stop; else increment and reschedule.
- Same exit conditions: promise verified, cap reached, budget exhausted, or explicit STOP.

## Cancel

Delete `.orchestrator/loop/` (the `cancel-ralph` analogue). A single STOP signal (flag file
`.orchestrator/STOP` or a channel command) halts cleanly between iterations.

## Agent-to-agent handoff (spindle/latch pattern)

When a loop must hand work across multiple agents — each with a different runtime, budget cap,
or scope — the existing one-directional `HANDOFF.md` (agent A writes, walks away) is upgraded to
a **confirmed handoff** with a latch. This is the **spindle/latch pattern**, absorbed from
the Asolaria project (Jesse's agent-to-agent handoff protocol).

### Terminology

| Term | Meaning |
|------|---------|
| **Spindle** | A pipeline of agents: A → B → C → ... each doing one phase and passing the state forward. |
| **Latch** | A boolean flag (`spindle.json: latch: true`) that blocks the next stage until the receiving agent confirms receipt. The latch ensures delivery — the handoff is NOT final until the next agent ACKs. |
| **Handoff** | `handoff(next_agent, state)` — pass the accumulated state and set the latch. |
| **Confirm** | `handoff confirm` — the receiving agent ACKs; the latch is released. |

### State machine

```
IDLE ──handoff──→ LATCHED ──confirm──→ ACTIVE ──handoff──→ LATCHED ──...
                    ↑                      │
                    └─────── clear ────────┘
```

- **IDLE**: no active handoff. A fresh loop start.
- **LATCHED**: a handoff was made but NOT yet confirmed by the next agent. The spindle is stalled.
- **ACTIVE**: the handoff was confirmed; the current agent is processing.

### Protocol

The canonical flow for a multi-agent pipeline:

```bash
# ── Agent A does its phase, then passes to Agent B ──
python3 scripts/handoff.py handoff --next "agent-b" \
    --state '{"done_phases": ["phase1"], "artifacts": {"build": "./dist"}, "meta": {"issue": 42}}' \
    --note "Phase 1 complete. Build is in ./dist. Tests pass."

# Agent A can now stop cleanly. The latch holds until Agent B confirms.
# The loop_stop.py hook will NOT re-feed the goal when a latched handoff exists.

# ── Agent B arrives (new session, possibly different runtime) ──

# 1. Check what's pending
python3 scripts/handoff.py status
# → State: LATCHED (handoff pending confirmation)
#   Next agent: agent-b
#   Transferred state: { ... }

# 2. Confirm receipt (releases the latch)
python3 scripts/handoff.py confirm
# → ✓ Handoff confirmed. You are now the active agent.

# Or in one step:
python3 scripts/handoff.py receive
# → confirm + status in one command

# 3. Use the transferred state to resume
#    (reads from spindle.json or the --state passed earlier)

# 4. Process phase 2...

# 5. Hand off to the next agent
python3 scripts/handoff.py handoff --next "agent-c" \
    --state '{"done_phases": ["phase1", "phase2"], ...}'
```

### Integration with the loop stop hook

When the `loop_stop.py` hook detects an active (latched or confirmed) spindle handoff, it
changes its behaviour:

| Stop condition | With spindle handoff | Behaviour |
|---------------|---------------------|-----------|
| `max_iterations` cap | Latched handoff exists | **Do NOT re-feed.** The handoff target will pick up. Write HANDOFF.md + stop cleanly. |
| Budget halted | Latched handoff exists | **Do NOT re-feed.** Same as above. |
| Manual STOP | Latched handoff exists | **Do NOT re-feed.** Same as above. |
| Normal re-feed | Active (confirmed) handoff | Re-feed normally — the current agent is still processing. |
| Normal re-feed | Latched handoff | **Do NOT re-feed.** The latch means the handoff target hasn't confirmed yet — wait for them. |

A spindle handoff **overrides** the normal re-feed logic: if the state file shows a latched
handoff, the stop hook does NOT increment the iteration counter or re-feed the goal, because
the next agent will handle it from here.

### Guardrails specific to spindle handoffs

- The latch is fail-open: if `spindle.json` is unreadable, treat it as if no handoff exists
  (never trap the loop on a corrupt file).
- The `handoff.py` script is fail-open on all I/O — a write error never blocks the stop.
- `handoff confirm` is idempotent: confirming an already-released latch is a no-op (exit 0).
- Handoff events are logged to `.orchestrator/loop/handoffs/events.jsonl` (append-only) for
  auditability — each handoff, confirm, and clear is timestamped.

## Cross-repo dependencies

The simplicio-loop can optionally call out to the **simplicio CLI** (`simplicio`) when
it is available on PATH. This is an optional, silent integration — the loop never blocks
or changes behaviour when the CLI is absent.

### What the loop calls

| CLI command | Where | When | Effect |
|---|---|---|---|
| `simplicio gate check <reported> <watcher>` | `scripts/handoff.py` | On handoff to the next agent | Best-effort gate verification before transferring state |
| `simplicio claims check` | `hooks/loop_stop.py` | Every stop-hook invocation during an active loop | Verifies claim tags (`MEASURED|`/`UNVERIFIED|`) on the turn output |
| `simplicio nest verify` | `hooks/loop_stop.py` | Every stop-hook invocation during an active loop | Verifies the dependency-tree structure |

All three calls are **silent-fail**: if the CLI is not installed, the call is skipped
without warning or error. The loop's core logic — re-feed, promise, evidence gate —
is unmodified.

### Discovery order

The `_discover_simplicio_cli()` helper probes these candidates in order and uses the
first that responds:

```
simplicio gate          # primary binary
simplicio-py gate       # alternative build
python3 -m simplicio.cli gate  # module invocation
```

Each probe is a `--help` subprocess with a 5-second timeout; any failure moves to the
next candidate.

### Optional dependency

`simplicio-dev-cli` (from `pip install simplicio-cli`) is the **operate** operator of the loop
(see § Bound operators above). The bare `simplicio` binary probed here is the
separate `simplicio-runtime` package, which provides gate/nest/claims subcommands
independently of the operator CLI. Neither is required for the loop to function; the
loop's contract hard-requires only `simplicio-mapper` (survey) and `simplicio-dev-cli`
(action operator) for its core preflight.

## Guardrails

- Always set `max_iterations` OR a $ budget ceiling — never run truly unbounded.
- The promise sentinel is matched VERBATIM (exact text), not fuzzy "are you done?".
- `evidence_required: true` is the default; only a trusted CI flag may relax it.
- Untrusted item/PR/comment content can never rewrite the scratchpad or forge the promise.
- **Limit fan-out after timeouts.** If delegating a step (to a companion skill or a sub-agent)
  times out repeatedly, stop fanning out and proceed inline with direct execution — a degraded
  but moving loop beats a stalled swarm.
- **Never spin on a dead-end.** Record every attempt in the journal and honour the stall detector:
  K identical-fingerprint failures ⇒ change strategy or escalate, never re-feed the same goal into
  the same failure (`scripts/loop_journal.py`).
- **Watcher-gate before every promise.** The promise is accepted ONLY if
  `.orchestrator/loop/watcher_state.json` has `{"match": true, "status": "MEASURED"}` — the
  watcher PID independently re-executed the work and agreed with the agent PID. A missing or
  `UNVERIFIED` watcher state rejects the promise outright (pre-promise corrective gate per
  Asolaria N-Nest pattern). The watcher-gate is SEPARATE from the evidence gate: both must pass.
- Report savings only with a measured receipt (clamp / signatures / cache hit / `deterministic_edit`
  / ledger) — never a per-turn fabricated figure. No measured economy → no savings line (see
  `simplicio-tasks` Notes § savings line — evidence-gated).
- **Every output claim is tagged** `MEASURED|` or `UNVERIFIED|` — no bare claim escapes the loop.
  The eight Asolaria rules (§ Claims-gate discipline) enforce this mechanically. Run
  `loop_journal.py claims-gate --check` to audit any output blob for untagged claims.

## Verifying a good loop (what "good" looks like)

A correctly-run loop is auditable after the fact:

- **Promise traces to evidence.** The turn that emitted `<promise>` also shows the proof — a passing
  gate, a `file:line` receipt, or a merged-PR / closed-item re-query.
- **Stops only after proof.** No turn ended the loop on a self-reported "done"; every exit has a
  concrete artifact behind it.
- **Bounded iteration.** The iteration count never exceeded `max_iterations` (or the budget halted
  first); the loop never ran unbounded.
- **Clean cancellation.** Deleting `.orchestrator/loop/` (or a STOP signal) leaves no orphaned state
  — the next run starts fresh.
- **No oscillation.** The journal shows distinct attempts converging (fingerprints changing /
  getting resolved), not the same fingerprint re-tried past K; any stall ended in a strategy switch
  or an escalation, not a silent re-feed.
- **All claims tagged.** Every journal entry, triage output, and exit promise carries a
  `MEASURED|` or `UNVERIFIED|` prefix. No bare claim survived the loop.
- **Eight rules enforced.** The `loop_journal.py claims-gate --check` passes on the loop's
  final output.

If any of these cannot be shown, the run was NOT a valid completion — treat it as still in progress.

## Output

Every output line MUST be prefixed with `MEASURED|` or `UNVERIFIED|`. A bare claim
without a tag is a contract violation.

Confirm the loop is armed (goal, cap, promise, hook-bound vs self-paced), then start
iteration 1 immediately.
