# Extension points — the 44 named binding points

These are the named points where work happens. For each, if the host runtime exposes a native
capability, BIND it (deterministic, local-first, near-zero token). If not, the LLM performs the
fallback with standard tools. The skill depends on the ABSTRACTION, never on a specific runtime.

| Extension point | What it does | LLM fallback (always available) |
|---|---|---|
| `orient` | Compressed repo/work map | `rg` / `git grep` / `git log --oneline -10`, read few files. Also: `.understand-anything/knowledge-graph.json` (Understand Anything) for rich structural graph + semantic search + guided tours |
| `recall` | Prior decisions / precedents | read ADRs / git history / past PRs |
| `normalize` | Work-item → canonical schema | LLM maps fields by hand |
| `deterministic_edit` | Mechanical file writer (zero-token apply of a decided change) | LLM applies edit with file tool |
| `autoscale` | Safe fleet size from machine profile | formula in orchestration.md |
| `plan` / `decide` | Plan / decision support | LLM reasons it out |
| `execute` | Local agent fan-out for mass/mechanical work | LLM does it or spawns host sub-agents |
| `issue_factory` | Full orchestrator loop: discover→claim→implement→PR | manual pipeline (Steps 2–6) |
| `claim` | Atomic claim on a work-item (cross-session safe) | `gh label "in-progress"` + lockfile |
| `worktree` | Per-item isolated checkout | `git worktree add` |
| `diagnostics` | Parse build/test output → structured errors → iterate-until-green | run the test, read the log, fix |
| `validate` / `smoke` | Run-verification ("works, not just compiles") | invoke binary directly, run affected tests |
| `pr` / `evidence` | PR open/update + verifiable evidence ledger | `gh pr` + receipt file |
| `watcher` | Durable scheduler / poller (survives reboot) | OS cron / scheduled task / session loop |
| `savings_ledger` | REAL token spend tracking per session | estimate `ceil(chars/4)` |
| `capability_rank` | Rank which skill/tool fits a sub-task | LLM picks |
| `compress` | Context compression / output clamping | summarize to bullets, head+tail clamp. Also: headroom proxy (`pip install headroom-ai`) for transparent 6-algorithm compression via daemon |
| `trajectory` | Record run outcome for self-improvement | manual log |
| `learn` | Learn from run — update precedents / memory | manual ADR |
| `human_gate` | Async human approval channel | ask user inline |
| `shell_exec` | Clamped shell execution (structured output, bounded size) | Bash with `\| head -N` |
| `retry` | Classified retry+backoff by failure class | manual retry loop |
| `status` | Live observability dashboard | `gh` queries |
| `security` | Supply-chain / secret scan | `rg` for secrets |
| `intake` | Ingest work from sprint/board link | `gh issue list` |
| `dependency_graph` | Inter-item ordering as a resumable DAG (B after A; independents fan out); re-run skips done nodes | LLM topo-sorts by depends-on/blocked-by, runs ready first, journals done node-ids to resume |
| `durable_workflow` | Per-item pipeline (intake→plan→edit→validate→deliver) as a resumable phase state-machine; retry skips done phases | LLM drives phases, journals which phase each item reached, resumes from last completed |
| `work_queue` | Durable priority queue that runs+auto-retries+requeues-stuck, with a write-serialization lock for shared checkouts | LLM keeps queue in JSONL/SQLite, pops by priority, re-enqueues on fail, lockfile+TTL guards shared-tree writes |
| `resource_governor` | Dynamic mid-loop throttle: decide when to back off + machine-tier ceilings before scaling a wave | LLM re-probes CPU/RAM/load each tick, reduces fleet / sleeps longer under load, degrades tiers |
| `delivery_gate` | One DoD gate: AC check + run-verification + regression guard + diff self-review + delivery certificate | LLM walks the AC checklist, runs affected tests, reviews own diff, writes a certificate into the receipt |
| `action_gate` | Risk-classify every mutation (safe/auto/ask) vs allow/deny + hardline blocklist before it runs | LLM pattern-matches action vs irreversible-op list, secret-scans, proceeds/auto-runs/escalates to `human_gate` |
| `repo_conventions` | Discover repo-specific conventions: CONTRIBUTING.md branch rules, commit scopes, AGENTS.md dev policies, PR template structure, CI commands beyond toolchain_detect (scripts/run_tests.sh, check-windows-footguns.py), cross-platform checks, supply-chain audit steps. Output structured object before Step 2 begins. | LLM reads CONTRIBUTING.md + AGENTS.md + .github/ + pyproject.toml, emits structured conventions summary that shapes Step 4–6 behavior (branch naming, commit scopes, PR checklist, CI commands, lint/test gates). |
| `reuse_precedent` | Match item by fingerprint to a prior SOLVED run → reuse not regenerate → ingest the new solution back | LLM greps past PRs/closed issues/solved-patterns journal for the fingerprint, applies it, appends new solution |
| `source_adapter` | Uniform source connector contract (list_ready/get_details/claim/update/attach/close) bound per source | LLM calls the source CLI/REST per verb; lockfile/label claim with TTL for cross-session safety |
| `prompt_budget` | Token-budgeted prompt envelope + prompt-fragment cache: assemble only what fits the per-task ceiling | LLM caps per-subtask context to a fixed budget (chars/4), trims to the few files that matter, small on-disk cache |
| `model_route` | Pick cheapest viable substrate per sub-task (L0 deterministic→local→mid→reasoning→paid), escalate only on need. LMCache KV cache (`pip install lmcache`) — acelera inferência local reduzindo TTFT via cache KV entre turnos do loop | LLM applies the tier table: mechanical→L0, mass→local, normal→mid, LARGE/CRITICAL/security→reasoning |
| `model_preflight` | Probe a usable model substrate is present+healthy before routing generation; else fail-fast or next tier | LLM pings endpoint / confirms local model+runner with a trivial call; on fail picks next tier or stops |
| `toolchain_detect` | Detect which build/lint/typecheck/test toolchains the repo actually has so validate/diagnostics route right | LLM inspects manifests/lockfiles/config + probes PATH to pick the correct toolchain per stack |
| `checkpoint_restore` | Snapshot run/repo state before a risky batch; restore to known-good if validation/delivery fails | LLM tags a commit / stashes / copies the journal before destructive ops, restores on failure |
| `notify` | Push progress/blocker/digest to a human channel + receive inbound approvals (async approval I/O) | LLM writes digest/approval-request to a file or session; no-reply = block the destructive op (headless rule) |
| `endpoint_compare` | Compare web/API/agent surfaces to detect drift; gaps become follow-up items (full-stack coverage) | LLM lists routes on each side (grep handlers / read OpenAPI) and diffs by hand to flag mismatches |
| `web_verify` | Drive a real browser (navigate/click/console) to prove a UI/web change works end-to-end; capture screenshot+trace as evidence | Playwright via `playwright-mcp` or headless `npx playwright` / `pytest-playwright`; evidence = artifact path, not pixels (see web-evidence.md) |
| `video_evidence` | Render a deterministic MP4 demo video of a screen/feature as evidence (and fulfil explicit "make a demo video" requests) | **hyperframes** (`npx hyperframes render` — heygen-com/hyperframes): assemble the `web_verify` screenshots into a captioned MP4 walkthrough; evidence = MP4 path, not bytes (see video-evidence.md). Fallback: stitch the PNGs with `ffmpeg` |
| `web_research` | Fetch current external knowledge (docs/CVE/version/SDK error), gated behind local-memory-miss, with provenance | LLM uses built-in web search/fetch only after local miss; records source URL as provenance |
| `transform_guard` | Verify a compaction preserved every code/URL/path/version token (fail-closed to original) | LLM extracts both token sets and compares by hand |

Rule: any change already DECIDED goes through `deterministic_edit` — never hand-write a file body
or regenerate it with a model when a mechanical apply exists. Reach for a paid model only for
genuine reasoning the deterministic layer cannot do (model routing in orchestration.md).

## simplicio-loop's bound operators (REQUIRED for the loop drive)

The `simplicio-loop` companion skill is NOT runtime-optional about two of these points — it binds
them to two installed CLIs (hard deps of `pip install simplicio-loop`) and BLOCKS if absent:

| Point(s) | Bound CLI | What replaces the LLM fallback |
|---|---|---|
| `orient` / `recall` | `simplicio-mapper` (`simplicio-mapper index . --json`) | the repo SURVEY — `.simplicio/*.json` (project-map, precedent-index, symbol-index, call-graph) instead of ad-hoc LLM reads |
| `execute` / `deterministic_edit` / `validate` / `diagnostics` | `simplicio-dev-cli task` (binary `simplicio-dev-cli`, pkg `simplicio-cli`) | the OPERATOR — applies a decided change via its 6-layer contract (mapper→precedent→prompt→diff→test→verify, ≤3 retries); the AI never hand-writes the diff inside the loop |

This is the one place the abstraction is realized by a REQUIRED binding rather than an optional
one. Everywhere else the inverted-dependency rule below still holds. See
`.claude/skills/simplicio-loop/SKILL.md` (§ Bound operators).

A host runtime MAY detect that this skill is running (by name) and auto-bind its native commands
to these points — transparently, at near-zero token cost — without the skill ever naming that
runtime. The binding lives in the host runtime, not here. This is the INVERTED DEPENDENCY: the
skill stays universal; the runtime injects the speed.
