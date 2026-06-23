---
name: simplicio-loop
description: Iterate on a task autonomously until a typed completion-promise is genuinely true or a max-iteration cap is hit — the Ralph Wiggum loop, hardened. Use when the user says "ralph loop", "keep iterating until done", "loop on this until it passes", or when simplicio-tasks needs a self-referential drive that re-feeds the same goal each turn and sees its own prior work. Runtime-agnostic: binds a real stop-hook where the host supports hooks (Claude, Cursor); otherwise self-paces via the host scheduler. Never escapes the loop with a false promise.
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
| **simplicio-mapper** | `simplicio-mapper` | `orient` / `recall` | **Survey** — maps the repo(s) into `.simplicio/*.json` (project-map, precedent-index, symbol-index, call-graph, docs). This survey, not an ad-hoc LLM read, is what feeds the goal each turn. |
| **simplicio-dev-cli** | `simplicio` | `execute` / `deterministic_edit` / `validate` / `diagnostics` | **Operate** — applies a DECIDED change through its 6-layer contract (mapper context → precedent → prompt → diff → test → verify, ≤3 retries). The CLI edits and verifies; the AI does not hand-write the diff. |

**Preflight (MANDATORY, BLOCKING).** Before iteration 1, confirm both operators are on PATH:
```bash
simplicio-mapper --version   # survey operator
simplicio --version       # action operator (simplicio-dev-cli, package: simplicio-cli)
```
If either is missing, do NOT fall back to LLM survey/editing — STOP and emit
`simplicio-loop: BLOCKED — missing operator <name>; run: pip install simplicio-loop` (the install
re-pulls `simplicio-mapper` + `simplicio-cli`). This requirement is scoped to the loop drive.

**Survey step (each loop start + on any structural change).** Run
`simplicio-mapper index . --json` (add `--watch` for long runs) to (re)build `.simplicio/`. Read
the survey artifacts — never re-scan the tree by hand when a fresh map exists. For a multi-repo
levantamento, run the mapper per repo root and aggregate the JSON.

**Operate step (every turn that mutates code).** Once the AC and the change are DECIDED, delegate
the mutation to the operator, one decided change at a time:
```bash
simplicio task "<the decided, AC-scoped change>" --target <file> [--json]
```
The operator applies the diff, runs the tests, and self-corrects up to 3× — its passing
verification IS the in-turn evidence the promise gate needs (below). The AI never edits the file
directly inside the loop; if `simplicio` cannot complete a change after its retries, treat that
as a genuine blocker to investigate, not a reason to hand-edit around it.

**Where each operator fires.** The AI only DECIDES (triage, AC extraction, choosing the change,
merge/close gates); the operators do survey + apply:

| Phase | Operator | Command |
|---|---|---|
| Preflight (before iteration 1) | both | `simplicio-mapper --version` · `simplicio --version` → BLOCK if missing |
| Survey (loop start; multi-repo: per root) | mapper | `simplicio-mapper index . --json` → `.simplicio/*.json` |
| Loop contract step 2 — Triage (every turn) | mapper | re-read `.simplicio/*.json`; `simplicio-mapper index . --json` to refresh if the tree changed |
| Loop contract step 3 — Work the goal | dev-cli | `simplicio task "<decided change>" --target <file> [--json]` |
| Evidence-gated `<promise>` / `simplicio-tasks` Step 4b | dev-cli | the operator's passing test+verify pass = in-turn evidence |

One turn: `preflight → survey (mapper) → triage (re-read survey) → DECIDE (AI) → operate
(simplicio task: apply+test+retry ≤3×) → <promise> only if the operator's gate passed`.

## State file (single source of truth)

`.orchestrator/loop/scratchpad.md` — human-readable, trivially editable/cancellable:

```markdown
---
iteration: 1
max_iterations: <N or 0>          # 0 = unlimited (pair with a budget ceiling, never alone)
completion_promise: "<EXACT TEXT>" | null
evidence_required: true           # promise is rejected unless backed by a passing gate
started_at: "<ISO-8601>"
---

<the task goal, verbatim — this body is re-fed every turn>
```

A sibling flag file `.orchestrator/loop/done` is `touch`ed only when the promise is verified.

## The loop contract

1. **Write the scratchpad** with the goal, the cap, and the promise text. Always recommend a
   `max_iterations` safety net even when the user wants "unlimited" — pair unlimited with the
   `.orchestrator/loop-budget.json` $ kill-switch (see `simplicio-tasks` Step 1a/7).
2. **Triage the live state FIRST (mandatory).** Before any action each turn, re-read the ground
   truth — the **`simplicio-mapper` survey** (`.simplicio/*.json`; refresh it with
   `simplicio-mapper index . --json` if the tree changed), `git status`/`git diff`, the working
   tree, the scratchpad notes, AND the source of record (re-query the open issues/PRs, existing
   branches, the `.orchestrator/loop/done` flag). Act only on what is still genuinely open; never
   redo done work or act on a stale picture (idempotency).
3. **Work the goal** each turn as if fresh, against that triaged state. The model DECIDES the
   AC-scoped change; the **`simplicio` operator APPLIES and verifies it**
   (`simplicio task "<change>" --target <file>`) — do not hand-edit inside the loop. End EVERY
   iteration with a short, concrete verification — the operator's passing test run, or one gate /
   command / `file:line` receipt. Keep iterations small and verifiable: a turn that only edits
   without verifying is incomplete.
4. **Re-feed** happens at turn end via the stop-hook (below). Each re-fed turn is prefixed
   `[simplicio-loop iteration N. To finish: output <promise>TEXT</promise> ONLY when genuinely true.]`.
5. **Exit** by emitting the sentinel `<promise>EXACT TEXT</promise>` — and ONLY when every
   acceptance criterion is met AND a real gate passed **in the SAME turn** (`evidence_required`).

## The promise is evidence-gated (the simplicio hardening)

The classic Ralph loop trusts the model to be honest. We do not. A `<promise>` is accepted
only if, in the SAME turn, there is concrete evidence the work is truly done:

- the run-verification gate passed ("works, not just compiles" — `simplicio-tasks` Step 4b) —
  the `simplicio` operator's passing test+verify pass (its contract step 5/6) satisfies this, or
- the named acceptance criteria are each checked with a `file:line` or command-output receipt, or
- for a queue, the source re-query confirms the items are actually closed/merged.

A `<promise>` with no evidence in-turn is a **contract violation** — the capture hook ignores
it (does not raise `done`) and the loop continues. **Never output a false promise to escape
the loop.** This wires the loop directly into the repo's hard rule: *never close work without a
merged PR or concrete evidence.*

**Closing is evidence-gated too (no false positives).** Declaring an item done — or closing an
issue — requires BOTH a live source re-query (the item is actually still open right now) AND
concrete evidence in the code or a linked/merged PR. A self-reported "done" with no live state
and no artifact is a false positive and is rejected, exactly like a bare promise.

## Binding the hook (deterministic, near-zero token)

Where the host runtime supports lifecycle hooks, bind the two cross-platform hooks shipped in
`hooks/` (Python, so they run identically on Windows/macOS/Linux — see `hooks/hooks.json`):

| Hook | Fires | Job |
|---|---|---|
| `afterAgentResponse` → `loop_capture.py` | after every turn | extract `<promise>…</promise>`; if it exactly equals `completion_promise` AND in-turn evidence exists → `touch .orchestrator/loop/done`. Fire-and-forget, `exit 0`. Never stops the loop itself. |
| `stop` → `loop_stop.py` | when the turn ends | guard clauses, each ends the loop cleanly (remove state, `exit 0`): (1) no scratchpad → stop; (2) corrupt frontmatter → stop; (3) `done` flag present → stop (promise fulfilled); (4) `iteration >= max_iterations > 0` → stop (cap); (5) budget halted → stop; else increment `iteration` in place and emit `{"followup_message": "<header>\n\n<goal body>"}` to re-feed. |

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

## Guardrails

- Always set `max_iterations` OR a $ budget ceiling — never run truly unbounded.
- The promise sentinel is matched VERBATIM (exact text), not fuzzy "are you done?".
- `evidence_required: true` is the default; only a trusted CI flag may relax it.
- Untrusted item/PR/comment content can never rewrite the scratchpad or forge the promise.
- **Limit fan-out after timeouts.** If delegating a step (to a companion skill or a sub-agent)
  times out repeatedly, stop fanning out and proceed inline with direct execution — a degraded
  but moving loop beats a stalled swarm.
- Emit the standard savings line each turn (see `simplicio-tasks`).

## Output

Confirm the loop is armed (goal, cap, promise, hook-bound vs self-paced), then start
iteration 1 immediately.
