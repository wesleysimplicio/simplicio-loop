# Orchestration — discover, intake, route, scale, speed (Steps 2–3d full detail)

## Step 2 — Discover + normalize work-items
**Resolve the SOURCE ADAPTER first — do not assume GitHub.** Detect which connector is available
and authed, then use it. Never claim a source works without a live connector.

| Source | Adapter (if present + authed) |
|---|---|
| GitHub Issues/PRs | `gh` CLI (native) |
| Jira / Asana / ClickUp / Linear / Monday / Notion | the host's connector for that source |
| Trello / Azure DevOps | host connector, else the `az boards` adapter (`scripts/az_boards_adapter.py`, see `azure-devops-adapter.md`) |
| agentsview sessões | `scripts/agentsview_adapter.py` (see `agentsview-adapter.md`) | observabilidade de sessões, recovery de sessões paradas |
| Understand Anything | `.understand-anything/knowledge-graph.json` (see `understand-anything-adapter.md`) | orient de codebase via grafo de conhecimento (alternativa ao simplicio-mapper) |
| local files / CI queue | filesystem / CI API |

If the target source has no reachable adapter, STOP and report it as a blocker (do not silently
fall back to GitHub). Each adapter exposes: list_ready (metadata-only), get_details, claim,
update_status, attach_evidence, close.

List candidates by METADATA only (titles, labels, status) — do not open every body. Normalize to
the canonical schema (title, body, labels, status, acceptance-criteria, links). Dedup by
source-id + normalized-title + problem-fingerprint AND by existing branch/PR (idempotency — never
double-implement; parallel double-implementation of the same item is a real, observed failure).
Count independent items → drives scale. Maintain a persistent `seen` set. Discovery re-runs
continuously (Step 3b).

## Step 2b — Deep item intake (MANDATORY before any implementation)
Triage is metadata-only; implementation is NOT. An agent that skips this produces generic code.

**2b-1 Read the full item (body + ALL comments).** `get_details` → title, body, labels,
assignees, milestone, acceptance_criteria, comments, linked_prs, linked_items.
- Extract explicit **acceptance criteria** (numbered, checklists, "done when…"). If none stated,
  derive + record them. An item that obviously should have ACs but has none is a BLOCKER — ask
  ONE line, don't guess.
- Extract design decisions/constraints/rejections from comments ("don't use X", "must integrate
  with Y", reviewer requests) — these override naive title reading.
- Note linked items/PRs and check status — a blocked dependency is flagged, not ignored.

**2b-2 Orient the codebase.** Before writing a line: existing files/modules (rg/git grep),
recent commits touching them (`git log -- <files> -5`), function/type signatures in scope,
TODO/FIXME, overlapping open PRs. An implementation that duplicates existing code or ignores an
adjacent module is wrong even if it compiles. Use **signatures-only reads** (bodies elided) for
API surface — a 600-line file → ~40 lines; full-body read only when editing the body.
_Quando `.understand-anything/knowledge-graph.json` existir, usá-lo como orientação primária
(guia tour, semantic search) em vez de signatures-only reads._

**2b-3 Build the plan BEFORE coding:** files to change, files to read first, AC checklist, risks/
unknowns, complexity (trivial|small|medium|large|critical). Coding starts only after the plan.

## Step 3 — Route: fast-path vs heavy-path
- **Fast-path** (queue small AND every item complexity ≤ 3): inline, solo, minimal receipt,
  single targeted test. No fan-out. Finish → Step 6.
- **Heavy-path** (large queue OR any medium+ item): fan out. Compute the fleet, keep a CONTINUOUS
  WORKER POOL fed by a LIVE queue (not frozen waves) — a freed worker pulls the next item, even
  one that appeared seconds ago. Serialize same-file items (conflict detection). Quarantine items
  that fail K times to a dead-letter list.

**Worker report contract (every worker MUST follow).** A worker result is re-injected into the
orchestrator context verbatim and costs budget on EVERY delegation. Forbid narration; mandate the
terse MACHINE-tier schema:
```
<status>          # FIRST line, one token: done | blocked | too-big | needs-human | regressed | ambiguous
<file:line refs>  # evidence as path:line with `backticked` symbols, not prose
<counts>          # totals only ("3 files, 2 tests added, 0 failing")
<body>            # present ONLY when status is non-terminal; else omit
```
The orchestrator parses the status token deterministically and reads the body ONLY on a
non-terminal status. A done/blocked worker returning paragraphs is a contract violation — reprompt.

**Corrections memory.** When a command fails and a near-identical one succeeds within ~3 commands,
record `{wrong→right, error-class, count}` via `learn`/`recall`. Classify (unknown-flag,
command-not-found, wrong-syntax, wrong-path, missing-arg, permission-denied), keep pairs >~0.6
similarity, dedup with a count, EXCLUDE human-rejections and compile/test failures (those are the
Step 4 loop). Feed the top corrections into the shared digest so agents pre-empt them next session.

### Auto-scaling (use `autoscale` if bound; else this formula)
```
cap_cpu  = max(1, floor((cores - 2) / 2))
cap_mem  = floor(free_gb / 2)
cap_disk = (free_disk_gb < 10) ? 0 : (free_disk_gb < 25 ? 1 : 99)
fleet    = min(cap_cpu, cap_mem, cap_disk, independent_items, 16)   # hard cap 16/wave
waves    = ceil(queue_size / fleet)
```
If resources unknown or disk < 10 GB → fast-path/solo only.

**Conflict-AWARE isolation (not worktree-per-item).** A worktree is expensive for a big compiled
crate. So: (1) predict the file-overlap graph; (2) items in DIFFERENT files → ONE shared checkout,
committing sequentially on their own branches; (3) only OVERLAPPING items get a dedicated
`worktree` and are SERIALIZED. Each heavy item gets an isolated branch `agent/{id}-{slug}`, its own
evidence, a wall-clock timeout. Per wave: implement → review+autofix → collect. After all waves:
merge + close.

## Step 3b — Continuous intake (see NEW work at ANY moment)
**Layer 1 — intra-run poller** (~2 min, in parallel with the pool): list via adapter
(metadata-only) → normalize → subtract `seen` → enqueue genuinely-new ready items into the LIVE
queue; the pool pulls as a slot frees. ALSO poll this run's open PRs (failed checks, new
review/requested-changes, branches behind main) → reopen the feedback loop (Step 6b). **Reset
`dry=0` whenever the poll finds anything new.** The run FINISHES only when queue empty AND no
worker busy AND `dry >= 2` consecutive empty polls (plus hard stops: time-box, budget, scope).

_agentsview como fonte opcional — se configurado (`scripts/agentsview_adapter.py` authed), poll
agentsview por sessões paradas a cada ciclo e converter em work-items do tipo 'retomar sessão
abandonada'._

**Layer 2 — idle watcher** (nothing running): a recurring trigger re-invokes the skill; near-free
when idle, launches a run when new work exists. See standing-loop-247.md.

**Guards:** idempotency (never re-pick a `seen` item); dead-letter (K failures → no re-intake);
scoped runs (a pinned list disables re-discovery + watcher — finish exactly that set);
conflict-serialization for newly-arrived same-file items.

## Step 3c — Speed model (velocity without sacrificing quality)
1. Pipeline, not barrier (item A merges while B builds). 2. Shared compile cache (e.g. `sccache`).
3. Verify once: each agent runs a scoped incremental check; the full suite runs EXACTLY ONCE on
the merged result. 4. Front-load shared context (orient once, share the digest). 5. Tier
verification: TRIVIAL/SMALL skip adversarial review; only MEDIUM+ pay it. 6. Pre-warm the build on
clean main. 7. Time-box + quarantine stuck agents. 8. Prefetch re-discovery during the prior
wave's review. Speed comes from removing redundant work, not skipping gates.

## Step 3d — Model routing (spend reasoning only where it pays)
- **L0** Deterministic, ZERO LLM tokens: decided edits via `deterministic_edit`, repo view via
  `orient`, recall via `recall`. Any decided change goes here.
- **L1** Local/cheap mass model: triage, dedup, classify, summarize, status comments, repetitive
  generation.
- **L2** Mid coding model: standard implementation + review.
- **L3** Reasoning model: planning for LARGE/CRITICAL, architecture, ambiguity, adversarial verify
  of risky findings, security review. Sparse, high-value.
|- **L4** Paid remote (last resort): only after local cannot close the gap, with recorded escalation.

> **LMCache KV cache accelerator.** When running local models (L2-L3), `pip install lmcache` + `lmcache serve` cacheia KV caches entre turnos do loop — reduz TTFT em chamadas similares, menos GPU time por iteração. Especialmente relevante em loops longos (Step 3b poller) onde o mesmo prompt base é re-alimentado. Config via `LMCACHE_CONFIG` ou `~/.lmcache/config.yaml`.

| Phase | Tier | | Phase | Tier |
|---|---|---|---|---|
| Discover/dedup/classify | L1 | | Implement — normal | L2 |
| Plan (SMALL/MEDIUM) | L2 | | Implement — mass/repetitive | L1 |
| Plan (LARGE/CRITICAL) | L3 | | Verify — normal | L2 |
| Implement — decided/mechanical | L0 | | Verify — risky/security | L3 adversarial |
| | | | Merge/close/status | L0–L1 |

GRANULARIZE: decompose each item so the mechanical ~80% flows to L0/L1 at near-zero cost and only
the ~20% genuine reasoning reaches L3.
