# 🔁 simplicio-tasks — The Universal Looping AI Orchestrator

<p align="center">
  <img src="assets/simplicio-tasks-logo.svg" alt="simplicio-tasks" width="920" />
</p>

<p align="center">
  <a href="https://github.com/wesleysimplicio/simplicio-tasks/stargazers"><img src="https://img.shields.io/github/stars/wesleysimplicio/simplicio-tasks?style=social" alt="Stars"></a>
  <a href="https://github.com/wesleysimplicio/simplicio-tasks"><img src="https://img.shields.io/badge/skill-runtime--agnostic-39FF14" alt="Runtime-agnostic"></a>
  <a href="#-the-43-extension-points"><img src="https://img.shields.io/badge/extension%20points-43-00E08A" alt="43 extension points"></a>
  <a href="#-token-economy"><img src="https://img.shields.io/badge/tokens-up%20to%2096%25%20fewer-green" alt="Up to 96% fewer tokens"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
</p>

<p align="center">
  <a href="#-tldr">TL;DR</a> ·
  <a href="#-vs-caveman--rtk">vs caveman & rtk</a> ·
  <a href="#-the-43-extension-points">43 Points</a> ·
  <a href="#-everything-inside">Everything Inside</a> ·
  <a href="#-install--use">Install</a>
</p>

<p align="center">
  <strong>🌍 Languages:</strong><br>
  <a href="README.md">🇬🇧 English</a> |
  <a href="READMEs/README.pt-BR.md">🇧🇷 Português</a> |
  <a href="READMEs/README.es-ES.md">🇪🇸 Español</a> |
  <a href="READMEs/README.fr-FR.md">🇫🇷 Français</a> |
  <a href="READMEs/README.de-DE.md">🇩🇪 Deutsch</a> |
  <a href="READMEs/README.it-IT.md">🇮🇹 Italiano</a> |
  <a href="READMEs/README.ja-JP.md">🇯🇵 日本語</a> |
  <a href="READMEs/README.ko-KR.md">🇰🇷 한국어</a> |
  <a href="READMEs/README.zh-CN.md">🇨🇳 简体中文</a> |
  <a href="READMEs/README.ru-RU.md">🇷🇺 Русский</a> |
  <a href="READMEs/README.pl-PL.md">🇵🇱 Polski</a> |
  <a href="READMEs/README.tr-TR.md">🇹🇷 Türkçe</a> |
  <a href="READMEs/README.nl-NL.md">🇳🇱 Nederlands</a> |
  <a href="READMEs/README.hi-IN.md">🇮🇳 हिन्दी</a> |
  <a href="READMEs/README.ar-SA.md">🇸🇦 العربية</a>
</p>

---

## ⚡ TL;DR

**simplicio-tasks** is a single, runtime-agnostic **skill** that turns any strong LLM
(Claude, Codex, Copilot, Gemini, Grok, local models) into an **autonomous looping
orchestrator**. You point it at a body of work — *"finish all the open issues"*,
*"clear the CI queue"*, *"drain the Jira board"* — and it runs the whole lifecycle
on its own:

> **discover → understand → decide → act → verify → correct → record → repeat**

It discovers work from any source, dedups, auto-scales an agent fleet to your
machine, implements each item through a quality loop that **runs the code (not just
compiles it)**, opens PRs, resolves CI/review feedback, merges, and keeps watching
**24/7** for new work — all behind safety gates and a hard cost kill-switch.

It carries **43 named extension points**. Each has an always-works LLM fallback, and
each *binds to a host runtime's native command* when one is present — making the
step deterministic and near-zero-token. **The skill names no runtime; the runtime
detects the skill.** That inversion is the whole trick: one universal protocol, with
optional native speed injected underneath.

```text
/simplicio-tasks termine as issues abertas
→ identity + pre-flight (kill-switch, auth, watcher)
→ discover 50 issues · dedup · build dependency DAG
→ autoscale fleet = 14 · pipeline implement→review→merge
→ each item: read body+ACs → orient code → plan → edit → run → verify → PR
→ merge · close with evidence · rollback if main breaks
→ keep polling every ~2 min for new work
```

---

## 🆚 vs caveman & rtk

simplicio-tasks was built **after deeply studying** the two best token-savers on
GitHub — [**caveman**](https://github.com/JuliusBrussee/caveman) (74k★, *compress the
talk*) and [**rtk**](https://github.com/rtk-ai/rtk) (63k★, *compress the commands*).
It folds the best of **both** into a full orchestrator. They reduce tokens;
simplicio-tasks **does the work** and reduces tokens while doing it.

| | 🪨 **caveman** | ⚙️ **rtk** | 🔁 **simplicio-tasks** |
|---|---|---|---|
| **What it is** | Claude Code skill | Rust CLI proxy | Runtime-agnostic skill |
| **Core idea** | Talk terser (drop filler) | Reduce dev-command output | **Orchestrate the whole job** |
| **Scope** | LLM prose output | Shell command output | Full work lifecycle, end to end |
| **Token savings** | ~65% on replies | 60–90% on commands | Both — catalog + caps + clamping |
| **Does the work?** | ❌ formatting only | ❌ proxy only | ✅ discover→implement→merge→close |
| **Multi-step autonomy** | ❌ | ❌ | ✅ continuous worker pool |
| **Quality gates** | — | — | ✅ AC gate · run-verification · adversarial verify · delivery gate |
| **Safety** | — | semgrep, disclaimers | ✅ 4-state verdict · attestation · secret-scan · human gate · kill-switch |
| **24/7 loop** | ❌ | ❌ | ✅ durable watcher, self-healing |
| **Runtime binding** | Claude/Codex/Gemini | any (PATH proxy) | **any** (43 extension points) |
| **What we took** | terse worker reports, density tiers, never-paraphrase guard, honest baseline | per-command reduction catalog, signal-tiered caps, compound-clamping, fail-open, 4-state verdict | — |
| **What we left** | grammar word-dropping (degrades code quality) | per-language registries (runtime-specific) | — |

> We **rejected** caveman's "talk-like-caveman" word-dropping on purpose — terse
> *prose* is fine, but mangling grammar degrades code and confirmations. We kept the
> *discipline* (never paraphrase code/URLs/paths), not the gimmick.

<p align="center">
  <img src="assets/architecture.svg" alt="architecture" width="900" />
</p>

---

## 🧩 The 43 extension points

Every step of work happens at a **named extension point**. If a host runtime exposes
a native capability it **binds** (deterministic, near-zero token). Otherwise the LLM
performs the **fallback** with standard tools (shell, git, gh, file edit, web). The
skill depends on the abstraction, never on a specific runtime.

### Orchestration & scale
| Point | What it does |
|---|---|
| `orient` | Compressed repo/work map |
| `normalize` | Work-item → canonical schema |
| `intake` | Ingest work from a sprint/board link |
| `source_adapter` | Uniform source connector (list/get/claim/update/attach/close) |
| `autoscale` | Safe fleet size from machine profile |
| `plan` / `decide` | Plan & decision support |
| `execute` | Local agent fan-out for mass/mechanical work |
| `issue_factory` | Full loop: discover→claim→implement→PR |
| `claim` | Atomic, cross-session-safe work-item claim |
| `worktree` | Per-item isolated checkout |
| `dependency_graph` | Resumable DAG ordering between items |
| `durable_workflow` | Per-item pipeline as a resumable phase state-machine |
| `work_queue` | Durable priority queue with auto-retry + write-lock |
| `resource_governor` | Dynamic mid-loop throttle + machine-tier ceilings |
| `model_route` | Cheapest viable substrate per sub-task (L0→remote) |
| `model_preflight` | Probe a usable model before routing generation |

### Editing, quality & evidence
| Point | What it does |
|---|---|
| `deterministic_edit` | Mechanical, zero-token apply of a decided change |
| `diagnostics` | Parse build/test output → structured errors → iterate |
| `toolchain_detect` | Detect the repo's real build/lint/typecheck/test stack |
| `validate` / `smoke` | Run-verification: "works, not just compiles" |
| `delivery_gate` | DoD: AC check + regression + diff review + certificate |
| `endpoint_compare` | Web↔API↔agent drift → follow-up items |
| `web_verify` | Drive a real browser to prove a UI change works |
| `pr` / `evidence` | PR open/update + verifiable evidence ledger |
| `retry` | Classified retry+backoff by failure class |
| `reuse_precedent` | Match a prior solved run → reuse, not regenerate |
| `trajectory` | Record run outcome for self-improvement |
| `learn` | Learn from a run — update precedents/memory |
| `status` | Live observability dashboard |
| `capability_rank` | Rank which skill/tool fits a sub-task |

### Tokens, context & safety
| Point | What it does |
|---|---|
| `recall` | Prior decisions / precedents |
| `compress` | Context compression / output clamping |
| `prompt_budget` | Token-budgeted prompt envelope + fragment cache |
| `shell_exec` | Clamped shell execution (structured, bounded) |
| `transform_guard` | Verify a compaction kept every code/URL/path/version token |
| `action_gate` | Risk-classify every mutation (safe/auto/ask) before it runs |
| `security` | Supply-chain / secret scan |
| `human_gate` | Async human approval channel |
| `notify` | Push progress/blocker/digest + receive approvals |
| `checkpoint_restore` | Snapshot state before a risky batch; restore on failure |
| `watcher` | Durable scheduler / poller (survives reboot) |
| `savings_ledger` | Real token-spend tracking per session |
| `web_research` | Fetch current external knowledge, gated, with provenance |

---

## 📦 Everything inside

A full inventory of what the skill carries — every mechanism, cited.

### The loop (7 steps + sub-steps)
- **Step 1** — Identity + cheap environment detection.
- **Step 1a** — Pre-flight: kill-switch budget, source auth, arm the watcher.
- **Step 1b** — The 43 extension points (bind native or LLM-fallback).
- **Step 1c** — Token-economy gate: `THINK / NO-THINK`, `INTERNET off by default`,
  `terminal-first execution`, **output-reduction catalog**, **signal-tiered caps**,
  **success-collapse + dedup**, **compound-command clamping**, **consumer-routed
  density tiers**, **fail-open**, **auto-clarity (safety overrides brevity)**.
- **Step 2** — Discover + normalize work-items (any source adapter).
- **Step 2b** — Deep intake: read full body + comments, extract **acceptance
  criteria**, **orient the codebase**, **signatures-only read mode**, build a plan.
- **Dependency DAG** (`dependency_graph` point) — resumable topological ordering between
  items, scheduled within Step 3.
- **Step 3** — Dual-path router: **fast-path** vs **heavy-path** continuous worker
  pool · **conflict-aware isolation** · **worker report contract** · **corrections
  memory**.
- **Step 3b** — Continuous intake: intra-run poller + idle watcher (see new work
  any minute).
- **Step 3c** — Speed model: pipeline (not barrier), shared compile cache,
  verify-once-at-merge, **shared context digest**.
- **Step 3d** — Model routing L0→L4 (deterministic → local → mid → reasoning → paid).
- **Step 4** — Quality loop · **AC gate (real DoD)** · **run-verification** ·
  **adversarial multi-vote verify** · **static-analysis gate**.
- **Step 5** — Safety gates: secret-scan, irreversible-op human gate, **4-state
  pre-execution verdict**, **per-segment compound attestation**, **trust-before-load
  config**, **supply-chain integrity gate**, **transform_guard**.
- **Step 6** — Deliver + close + self-audit · **evidence package** · **verify
  reality (never trust self-report)** · **rollback-guard if merge breaks main**.
- **Step 6b** — Close the feedback loop: CI → fix, review comments → resolve,
  branch-behind → reconcile, full **PR lifecycle** until merge-ready.
- **Step 7** — 24/7 standing loop (10 axes): durable driver, total coverage matrix,
  durable state, **cost governance + hard kill-switch**, unattended safety,
  self-healing + **intelligent retry by failure class**, prioritization/WIP,
  observability + **periodic savings audit** + **snapshot measurement**,
  self-improvement, coordination & clean stop.

### Token economy (folded in from rtk + caveman)
- Terminal-first execution — never simulate a command.
- **Cross-platform** substitution table (Windows / macOS / Linux): 30+ facts the
  terminal answers cheaper than the LLM.
- **Output-reduction catalog** as data: per-command recipe, expected-savings %,
  `skip-if-structured` guard.
- **Signal-tiered caps**: `CAP_ERRORS / CAP_WARNINGS / CAP_LIST / CAP_INVENTORY`.
- **Success-collapse** + **dedup-with-counts** (with an `unless errors` guard).
- **Compound-command clamping** — per-segment, pipe/redirect-safe, fail-open.
- **Density tiers by consumer** (machine vs human); skip already-dense content.
- **Worker report contract** — status-token-first terse schema for sub-agents.
- **Honest savings baseline** = realistic control arm, **bound to a passing quality
  gate** (compression that fails its gate earns zero credit).

### Quality & delivery
- Acceptance-criteria DoD checklist · run-verification · adversarial verify ·
  static-analysis gate · delivery certificate · reality re-verification ·
  automatic rollback.

### Safety
- Secret-scan · irreversible-op human gate · 4-state verdict (never escalate
  privilege) · compound-command attestation · trust-before-load · supply-chain
  integrity · prompt-injection hardening · hard $ kill-switch for unattended runs.

### 24/7 autonomy
- Durable scheduler · live queue + idle watcher · durable journal/state ·
  circuit breakers · dead-letter quarantine · self-improvement & meta-review ·
  multi-instance atomic claims · clean STOP signal.

---

## 🚀 Install & use

simplicio-tasks is a **skill** — a single folder you drop into any runtime that
loads skills. No dependency, no binary required.

```bash
# Claude Code (project or user skills dir)
git clone https://github.com/wesleysimplicio/simplicio-tasks
cp -r simplicio-tasks/.claude/skills/simplicio-tasks  <your-repo>/.claude/skills/

# then, in your agent:
/simplicio-tasks finish all the open issues
```

Other runtimes (Codex, Gemini, Copilot, local agents) load the same
`SKILL.md` — see [`AGENTS.md`](AGENTS.md), [`CLAUDE.md`](CLAUDE.md) and
[`GEMINI.md`](GEMINI.md) for the per-runtime entry points. Where a host runtime
exposes native commands, it auto-binds them to the extension points; otherwise the
LLM fallbacks cover **100%** of the work.

**Before an unattended 24/7 run:** set a cost ceiling (`.orchestrator/loop-budget.json`,
`daily_usd_ceiling > 0`), confirm source auth is persistent, and keep the
irreversible-op human gate + secret-scan on. With `ceiling = 0` the watcher refuses
to run unattended (fail-safe).

---

## 📊 Token economy

Every message ends with an honest savings line:

```
simplicio-tasks: ~<spent> tokens · baseline ~<control-arm> · saved ~<saved> (<pct>%)
```

The baseline is the **cheapest sensible non-orchestrated path** to the same outcome —
not a verbose strawman — and savings are **only credited when the item's
run-verification and acceptance-criteria gate pass**. Raw compression is never
counted as success on its own.

---

## 📄 License

MIT — see [LICENSE](LICENSE). Part of the [Simplicio](https://github.com/wesleysimplicio) ecosystem.
