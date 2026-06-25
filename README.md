# 🔁 simplicio-tasks — The Universal Looping AI Orchestrator

<p align="center">
  <img src="assets/simplicio-loop-hero.jpg" alt="simplicio-loop" width="920" />
</p>

<p align="center">
  <a href="https://github.com/wesleysimplicio/simplicio-loop/stargazers"><img src="https://img.shields.io/github/stars/wesleysimplicio/simplicio-loop?style=social" alt="Stars"></a>
  <a href="#-the-11-skills--accelerators"><img src="https://img.shields.io/badge/skills-11-7C3AED" alt="11 skills"></a>
  <a href="#-source-adapters"><img src="https://img.shields.io/badge/source%20adapters-5-00E08A" alt="5 source adapters"></a>
  <a href="#-11-runtimes-one-protocol"><img src="https://img.shields.io/badge/runtimes-11-2563EB" alt="11 runtimes"></a>
  <a href="#-the-48-extension-points"><img src="https://img.shields.io/badge/extension%20points-48-00E08A" alt="48 extension points"></a>
  <a href="#-token-economy"><img src="https://img.shields.io/badge/tokens-up%20to%2096%25%20fewer-green" alt="Up to 96% fewer tokens"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
  <a href="https://discord.gg/wM6tr7xVb"><img src="https://img.shields.io/badge/Discord-Join%20Simplicio-5865F2?logo=discord&logoColor=white" alt="Join the Simplicio Discord"></a>
</p>

<p align="center">
  <a href="#-tldr">TL;DR</a> ·
  <a href="#-the-11-skills--accelerators">11 Skills</a> ·
  <a href="#-source-adapters">Source Adapters</a> ·
  <a href="#-11-runtimes-one-protocol">11 Runtimes</a> ·
  <a href="#-the-loop">The Loop</a> ·
  <a href="#-token-economy">Token Economy</a> ·
  <a href="#-token-economy">Capture Engine</a> ·
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

**simplicio-tasks** is a runtime-agnostic **super-plugin** — one autonomous looping
orchestrator (invoked as **`/simplicio-tasks`**) plus **five satellite skills** — that turns any
strong LLM (Claude, Codex, Copilot, Gemini, Cursor, local models) into a self-driving worker. You
point it at a body of work — *"finish all the open issues"*, *"clear the CI queue"*, *"drain the Jira board"* — and it
runs the whole lifecycle on its own:

> **discover → understand → decide → act → verify → correct → record → repeat**

It discovers work from any source (GitHub Issues, Jira, Azure DevOps, agentsview sessions, and
more), dedups, auto-scales an agent fleet to your machine, implements each item through a quality
loop that **runs the code (not just compiles it)**, opens PRs, resolves CI/review feedback, merges,
and keeps watching **24/7** for new work — all behind safety gates and a hard cost kill-switch.

```text
/simplicio-tasks finish all open issues
→ identity + pre-flight (kill-switch, auth, watcher)
→ discover 50 issues · dedup · build dependency DAG
→ autoscale fleet = 14 · pipeline implement→review→merge
→ each item: read body+ACs → orient code → plan → edit → run → verify → PR
→ merge · close with evidence · rollback if main breaks
→ keep looping every ~2 min until the queue is dry (evidence-gated, never a false "done")
```

Three things make it different: it is a **super-plugin of focused skills**, it runs the **same
protocol on 11 runtimes**, and it does all of this with **aggressive, honest token economy**.

<p align="center">
  <img src="assets/simplicio-loop-infographic.png" alt="simplicio-loop — the whole system at a glance: 6 core skills, 5 satellites, 5 accelerators, 48 extension points, 11 runtimes, up to 96% fewer tokens" width="920" />
</p>

---

## 📘 Official capability record (v3.10.0)

The complete, official roster of what `simplicio-tasks` ships — every capability below is **real,
runnable, and tested** (`python3 scripts/check.py`: claims-audit 4/4 + 27 tests). Each links to its
deep section and its worker.

| Capability | What it does | Proof / worker | Details |
|---|---|---|---|
| 🎬 **Video evidence** (`video_evidence`) | Renders a **deterministic MP4** demo of a screen/feature with [hyperframes](https://github.com/heygen-com/hyperframes) — fulfils `/simplicio-tasks make a demo video of screen X` and doubles as CI-reproducible proof a UI change works | `scripts/video_evidence.py` · BLOCKED (never fake-pass) without Node 22+/FFmpeg | [§ Video evidence](#-video-evidence--demo-videos-via-hyperframes) |
| 🧠 **Attempt memory + stall detector** | A durable run-journal (`.orchestrator/loop/journal.jsonl`) + a stall detector so the loop **changes strategy instead of oscillating**; incremental triage (`since`) reads only the delta each turn | `scripts/loop_journal.py` · `selftest` 9/9 | [§ Anti-oscillation](#-attempt-memory--stall-detector-anti-oscillation) |
| 🧭 **Repo conventions** (`repo_conventions`) | **Learns the repo's own playbook** — mines git history + merged PRs + static config into `.orchestrator/conventions.json` so every new branch/commit/PR mirrors the team's established style; worktree-per-item isolation is the default | `scripts/repo_conventions.py` · `selftest` 19/19 | [§ The full flow](#️-the-full-flow--from-demand-to-delivery) |
| 🔒 **Fail-closed safety gate** (`action_gate`) | A `PreToolUse`/git-pre-push hook that **mechanically blocks** force-push, history rewrite, mass-delete, destructive DDL, infra teardown, and secret-laden commits/pushes — Step 5 made executable, not prose | `hooks/action_gate.py` · `selftest` 15/15 | [§ Safety](#-safety-non-negotiable) |
| 🔬 **Local verification** | A test suite (worker selftests + an **e2e of the loop driver** proving evidence-gated exit) + a **claims-audit** (referenced scripts exist · counts consistent · `_bundle ≡ source`) — all local, **no paid CI** | `scripts/check.py` · `scripts/claims_audit.py` · `tests/` | [§ Tests & local checks](#-tests--local-checks-no-paid-ci) |
| ✅ **Honest savings** | The savings line is now **evidence-gated, not mandatory** — a number is shown only with a measured receipt (clamp/signatures/cache/`deterministic_edit`/ledger); never fabricated | token-economy contract | [§ Token economy](#-token-economy) |
| 💳 **Open-core billing** | A deterministic, privacy-preserving meter→invoice over the metering the loop already produces (kill-switch + `savings_ledger`) — three tiers (seat/run/metered) | `scripts/billing_aggregator.py` · `selftest` 11/11 | [PRICING.md](PRICING.md) |

Two loop **modes** make termination explicit: **converge** (a single hard task — ends on the
evidence-gated `<promise>` or a stall escalation) vs **drain** (a queue — ends when the source
re-query stays empty K rounds). Both still obey the universal exits (promise+evidence,
`max_iterations`, budget, STOP).

> Loop scoring across this line of work: **7.5** (strong design, unproven) → **9** (attempt memory +
> anti-oscillation) → **9.5** (reproducible local proof) → **~10** (enforced safety + complete loop
> semantics). The verification infra now catches the project's own regressions as it grows.

---

## 🧠 The 11 skills & accelerators

The orchestrator core + five satellites + five accelerators/integrations. Each satellite is
**optional** — when loaded, the orchestrator delegates to it (richer + cheaper); when absent, the
inline protocol covers 100%. Accelerators are **auto-detected** — present = used, absent = LLM
fallback.

| # | Capability | Absorbs | What it does | Token impact |
|---|---|---|---|---|
| 1 | 🔁 **simplicio-tasks** | — | The orchestrator loop: 48 extension points, dual-path router, self-audit convergence | Core |
| 2 | ♾️ **simplicio-loop** | [ralph-loop](https://github.com/cursor/plugins/tree/main/ralph-loop) | Hardened Ralph loop: evidence-gated `<promise>` exit, max_iterations cap | Loop drive |
| 3 | 🧱 **simplicio-orient** | [rtk](https://github.com/rtk-ai/rtk) + [caveman](https://github.com/JuliusBrussee/caveman) | Terminal-first execution, output-reduction catalog, tee-cache, signatures-read | L0 deterministic |
| 4 | 🔥 **simplicio-review** | [thermos](https://github.com/cursor/plugins/tree/main/thermos) | Parallel adversarial review on distinct rubrics → deduped verdict | Quality gate |
| 5 | 🗜️ **simplicio-compress** | [caveman](https://github.com/JuliusBrussee/caveman) | Output + memory compression, fail-closed `transform_guard` | 40-60% fewer |
| 6 | 🎓 **simplicio-learn** | [teaching](https://github.com/cursor/plugins/tree/main/teaching) | Post-run retrospective → durable, deduped lessons in memory | Smarter each run |
| 7 | 🧭 **Understand Anything** | [Egonex-AI](https://github.com/Egonex-AI/Understand-Anything) | Knowledge graph orient: semantic search, guided tours, dependency graph | **L0 zero tokens** |
| 8 | 📊 **agentsview** | [kenn-io](https://github.com/kenn-io/agentsview) | Session analytics, cost tracking, stalled-session discovery | **L1** SQL only |
| 9 | ⚡ **LMCache** | [LMCache](https://github.com/LMCache/LMCache) | KV cache between loop turns — 40-70% TTFT reduction on local models | GPU time ↓ |
| 10 | 🗜️ **Simplicio capture engine** | `engine/simplicio_engine.py` (native, stdlib-only; savings-schema compatible with the OSS [headroom](https://github.com/headroomlabs-ai/headroom) project) | Transparent capture proxy: forwards to the real provider, measures + deterministically compresses, writes `proxy_savings.json` | **deterministic** |
| 11 | 🎬 **video_evidence (hyperframes)** | [hyperframes](https://github.com/heygen-com/hyperframes) | Renders a **deterministic MP4** demo video of a screen/feature — fulfils `/simplicio-tasks make a demo video of screen X` AND doubles as CI-reproducible proof a UI change works | Evidence producer |

Each skill lives under [`.claude/skills/`](.claude/skills); each accelerator has a reference doc
under `.claude/skills/simplicio-tasks/references/` (the video producer:
[`video-evidence.md`](.claude/skills/simplicio-tasks/references/video-evidence.md), worker
[`scripts/video_evidence.py`](scripts/video_evidence.py)).

---

## 📡 Source adapters

The orchestrator discovers work from any source via pluggable adapters. Each exposes six verbs:
`list_ready`, `get_details`, `claim`, `update_status`, `attach_evidence`, `close`.

| Source | Adapter | Purpose |
|---|---|---|
| GitHub Issues/PRs | `gh` CLI (native) | Primary work-item source |
| Jira / Asana / ClickUp / Linear / Notion | host connector | Board/project management |
| Trello / Azure DevOps | `az boards` adapter | Azure work tracking |
| **agentsview sessions** | `scripts/agentsview_adapter.py` | Stalled session recovery + cost observability |
| Local files / CI queue | filesystem / CI API | Internal work tracking |

See each adapter's reference doc under `.claude/skills/simplicio-tasks/references/`.

---

## 🌐 11 runtimes, one protocol

One universal skill core + one set of hooks drives every runtime. An adapter is thin: it tells a
runtime *where to load the skills*, *how to arm the loop*, and *how to bind native speed*. **The
skill names no runtime; the runtime detects the skill.**

| Runtime | Skill load | Loop drive | Native bind |
|---|---|---|---|
| **Claude Code** | `.claude/skills/` + plugin | `Stop` hook | MCP |
| **Codex** | `AGENTS.md` | self-paced | MCP / adapter |
| **VS Code (Copilot)** | `copilot-instructions.md` | tasks | MCP |
| **Cursor** | `.cursor-plugin/` | `stop`+`afterAgentResponse` | MCP / rules |
| **Antigravity** | rules / `AGENTS.md` | self-paced | MCP |
| **Kiro** | `.kiro/steering/` | specs | MCP |
| **OpenCode** | `AGENTS.md` | self-paced | MCP |
| **Gemini** | `GEMINI.md` | self-paced | MCP / adapter |
| **Aider** | `CONVENTIONS.md` | self-paced | — (LLM fallback) |
| **Hermes** | native recall | native loop | **native** |
| **OpenClaw** | plugin SDK | native scheduler | **native** |

The promise: **same protocol, same gates, same safety on all 11 — only the speed differs.**
`orient_clamp.py` (token economy) works on every runtime with zero wiring. See
[`adapters/MATRIX.md`](adapters/MATRIX.md).

---

## 🗺️ The full flow — from demand to delivery

Every layer the orchestrator acts on, in order — from reading the demand (issues, tasks, assigns)
to delivering merged, evidenced work, then looping 24/7 for more.

```mermaid
flowchart TD
  subgraph SRC["1 · Demand sources (any adapter)"]
    direction LR
    S1["GitHub Issues / PRs / CI"]
    S2["Jira · Azure DevOps · Linear · ClickUp · Notion · agentsview · Understand Anything (orient)"]
    S3["Assigns · TODO/FIXME · CVE · local files · LMCache (inference accelerator)"]
  end
  SRC --> PF
  subgraph PF["2 · Pre-flight gates"]
    direction LR
    P1["cost kill-switch budget · agentsview cost check"]
    P2["source auth + scopes"]
    P3["arm 24/7 watcher"]
  end
  PF --> DISC
  subgraph DISC["3 · Discover + normalize"]
    direction LR
    D1["source_adapter: list metadata only"]
    D2["normalize to canonical schema"]
    D3["dedup id+title+fingerprint+branch/PR"]
    D4["dependency DAG"]
  end
  DISC --> INTK
  subgraph INTK["4 · Deep intake (per item)"]
    direction LR
    I1["body + ALL comments"]
    I2["extract acceptance criteria"]
    I3["orient code · signatures-only reads or Understand Anything knowledge graph"]
    I4["plan + AC checklist + complexity"]
  end
  INTK --> RT{"5 · Route"}
  RT -->|"small and every item complexity at most 3"| FAST["Fast-path: solo, one targeted test"]
  RT -->|"large queue or any medium+"| POOL
  subgraph POOL["6 · Continuous worker pool (autoscaled, conflict-aware)"]
    direction LR
    W1["claim · branch · worktree if overlap"]
    W2["deterministic_edit"]
    W3["quality loop: edit-lint-test-fix"]
  end
  FAST --> QG
  POOL --> QG
  subgraph QG["7 · Quality gates"]
    direction LR
    Q1["AC gate = real DoD"]
    Q2["WORKS not just compiles · web_verify (Playwright) · video_evidence (hyperframes MP4)"]
    Q3["adversarial review · thermos rubrics"]
  end
  QG --> SG
  subgraph SG["8 · Safety gates (non-negotiable)"]
    direction LR
    G1["secret-scan"]
    G2["irreversible-op human gate"]
    G3["4-state verdict · attestation"]
  end
  SG --> DEL
  subgraph DEL["9 · Deliver"]
    direction LR
    L1["commit · push · Draft PR"]
    L2["close in-source + evidence"]
    L3["verify reality, not self-report"]
  end
  DEL --> FB
  subgraph FB["10 · Feedback loop to merge-ready"]
    direction LR
    F1["CI fail -> fix root cause"]
    F2["review comments -> adjust"]
    F3["branch behind main -> additive rebase"]
  end
  FB -->|"merged and closed"| DONE(["done + evidence + measured savings (only if a receipt exists)"])
  WATCH["11 · 24/7 watcher · simplicio-loop evidence-gated promise · max-iterations cap · cost kill-switch · LMCache KV cache warm"]
  FB -. "poll new work / comments / checks" .-> WATCH
  DONE -. "idle until new work" .-> WATCH
  WATCH -. "re-feed the goal" .-> DISC
```

---

## 🔁 The loop

The **Evidence-Gated Loop** is the core mechanism. It re-feeds the same goal each turn so the
agent sees its own prior work. Exit is ONLY via:

1. **Evidence-gated `<promise>`** — the turn that emits the promise MUST also carry concrete
   proof (passing test, merged PR, closed-item re-query). A promise with no evidence = ignored.
2. **`max_iterations` cap** — hard safety backstop
3. **Budget kill-switch** — `daily_usd_ceiling` halts the loop when spent
4. **STOP signal** — `.orchestrator/STOP` or channel command

Between turns, LMCache (when available) caches the KV state so re-feed costs near-zero prefill.

### 🧠 Attempt memory + stall detector (anti-oscillation)

A re-feed loop that remembers nothing oscillates — try X, fail, try X again — until the cap burns.
simplicio-loop keeps a **durable run-journal** (`.orchestrator/loop/journal.jsonl`, append-only:
`iteration · action · hypothesis · gate · error-fingerprint`) and a **stall detector**
([`scripts/loop_journal.py`](scripts/loop_journal.py), deterministic + model-free):

- **Error fingerprint** — the failing gate output is reduced to a stable hash with line numbers,
  paths, hex/uuids, timestamps and durations normalized away, so the *same* bug is recognized
  across turns even when the incidental text differs.
- **Stall = K identical-fingerprint failures in a row** (default K=3). A changing fingerprint means
  the loop is moving (PROGRESS); the same one K times means it is spinning (STALLED).
- On STALLED the loop does **not** re-feed the same goal — it names the **dead-end actions** to
  avoid, then **switches strategy** or **escalates to the human gate** with the fingerprint.
- `loop_journal.py resume` is read at the top of every turn, so a fresh process continues without
  re-deriving prior attempts (real resume) and never retries a known dead-end.

```bash
loop_journal.py resume                       # what was tried + dead-ends to avoid
loop_journal.py record --iteration N --action "…" --gate fail --gate-output test.log
loop_journal.py stall --k 3 --exit-code      # PROGRESS → re-feed · STALLED → switch/escalate
```

---

## 🎬 Video evidence — demo videos via hyperframes

The loop can **create demonstration videos** of a screen/feature on request, and reuse that video
as proof a change works. The producer is [**hyperframes**](https://github.com/heygen-com/hyperframes)
(by HeyGen) — it renders HTML/CSS/media compositions to a **deterministic MP4** ("same input, same
frames, same output"), so the demo is a CI-reproducible artifact, not a throwaway recording. No API
keys; local render via headless Chrome + FFmpeg (Node 22+).

Two ways it fires — both via the `video_evidence` extension point (worker
[`scripts/video_evidence.py`](scripts/video_evidence.py), contract
[`references/video-evidence.md`](.claude/skills/simplicio-tasks/references/video-evidence.md)):

1. **On request — the video IS the deliverable.** Ask for it directly and the orchestrator routes
   the work-item to the hyperframes producer:

   ```text
   /simplicio-tasks make a demo video of the system login screen
   → detect: video-creation request  → drive the screen with web_verify (per-step screenshots)
   → scaffold a hyperframes composition  → npx hyperframes render → deterministic MP4
   → attach the MP4 to the PR as evidence + close with the link
   ```

2. **As proof — the video backs a code change.** After a UI change, the same MP4 walkthrough is the
   strongest "works, not just compiles" receipt (Step 4b) and a valid evidence-gated `<promise>`
   for the loop — a video that never rendered yields **BLOCKED**, never a fake pass.

The two evidence producers chain: `web_verify` (Playwright) captures the per-step screenshots,
`video_evidence` (hyperframes) assembles them into a captioned, deterministic MP4 walkthrough.
Evidence is always a **file path + boolean verdict** — never video bytes in context (token economy).

```bash
# one-shot, outside the loop
python3 scripts/video_evidence.py detect  --goal "record a video of the checkout screen"
python3 scripts/video_evidence.py verify  --name checkout-demo \
    --frames .orchestrator/tee/web --title "Checkout" --issue 42 [--upload --pr 42]
```

---

## 📊 Token economy

| Technique | Savings |
|---|---|
| `deterministic_edit` (L0) | 100% of edit tokens (file written mechanically, never by LLM) |
| Terminal-first execution | Facts from shell, not LLM hallucination |
| Output-reduction catalog | Caps per command type (`CAP_ERRORS=20`, `CAP_WARNINGS=10`, `CAP_LIST=20`) — `orient_clamp.py` |
| Tee+CCR cache on failure | Never re-run a failed command — read the cached output |
| Signatures-only reads | `simplicio-cli signatures <file>` — 870-line file → 65 lines (**93% saved**), bodies stripped |
| `simplicio-compress` | Terse prose + one-time memory compaction |
| `orient_clamp.py` | Clamp + tee on every shell command, zero wiring |
| Native response cache | repeated deterministic (temp=0) request → served from cache, skips the LLM call (**100% on hit**) — `simplicio-cli cache`, on by default (`SIMPLICIO_CACHE=0` to disable) |
| Simplicio capture proxy + MCP | 60-95% fewer tokens on tool outputs via a transparent compression daemon |

Savings only count on a verified-correct outcome. Baseline = the cheapest sensible non-orchestrated
path to the same result. **Savings reporting is evidence-gated, not mandatory:** a savings figure is
shown only when a turn actually ran an economy-producing command and the number traces to a
measured receipt (clamp tee, signatures-read, cache hit, `deterministic_edit`, `savings_ledger`).
No measured economy → no savings line; the orchestrator never fabricates a baseline or a percentage.
See `references/token-economy.md`.

### 🔎 Running `simplicio-tasks`: economy vs measurement (per runtime)

Two different things happen when you call **`simplicio-tasks`**, and they behave differently per runtime:

- **Economy** — compression, output clamps, signatures-only reads, `deterministic_edit` — applies **every
  time the skill runs and loads `simplicio-orient` / `simplicio-compress`, on any runtime.** It is the
  skill's behavior plus the hooks (strongest where hooks exist: `orient_clamp.py` auto-clamps on Claude and
  Cursor; elsewhere it is instruction-driven).
- **Measurement** — the Token Monitor's live numbers — only counts traffic that flows **through the
  capture proxy.**

| Runtime | Economy (skill) | Measurement (monitor) |
|---|---|---|
| **Hermes** | ✓ | ✓ **automatic** — already routed through the proxy (`base_url → :8788`) |
| **Claude** | ✓ (skill + hooks) | ✗ by default — Claude talks to `api.anthropic.com` directly; measured only once routed (`simplicio-cli wrap claude`, or `ANTHROPIC_BASE_URL → http://127.0.0.1:8788`) |
| **Codex** | ✓ (skill) | ✗ by default — `simplicio-cli init codex` adds the MCP tools but does not route LLM traffic; measured with `simplicio-cli wrap codex` or an OpenAI base-url pointing at the proxy |

So: the **savings happen on every runtime**; the **monitor tallies them automatically on Hermes**, and on
Claude/Codex after a **one-time routing step** (`simplicio-cli wrap …` / base-url → `:8788`). Without routing,
the economy still applies — the monitor just won't count those tokens. `scripts/simplicio-economy.sh wire`
does this routing for OpenAI-compatible clients at install time.

### 📈 Simplicio Token Monitor

A view of the savings you open when you want — only the capture is always-on:

- **Capture proxy** — **always-on** (the one auto-started service; the wired clients need it
  reachable). It silently captures + measures Claude + Codex + Hermes in the background.
- **Web dashboard** — `http://127.0.0.1:9090` — real-time token chart, savings gauge, the LLMs/runtimes
  and **141/144 providers (98%)** we intercept, a live proxy log. **Opens once on the first install**
  so you see it works, then it's **on-demand** — re-open it any of these ways:
  - `simplicio-loop dashboard` — works from anywhere after the pip install (no repo path needed);
    `simplicio-loop dashboard --stop` to close, `--no-browser` to just start the server.
  - `bash scripts/simplicio-economy.sh monitor` (repo checkout) · `… monitor stop` to close.
  - just **ask the agent** — "open the token dashboard".
- **Menu-bar / tray widget** — live tokens saved in the system tray (macOS rumps · Windows/Linux pystray).
  **On-demand:** `bash scripts/simplicio-economy.sh tray` · `… tray stop`.

Install auto-starts **only the capture proxy** (macOS launchd · Linux systemd · Windows Startup). The
dashboard opens **once** on a fresh install (marker-guarded — a re-install/update never reopens it; opt
out with `SIMPLICIO_NO_DASHBOARD=1`), and the tray never opens by itself — nothing is forced to stay
open. Manage the stack: `scripts/simplicio-economy.sh {status|up|monitor|tray|wire}`. After install,
capture runs **without invoking the loop** — see `references/token-capture.md`.

### 🛠️ The capture engine — one native module, every command

[`engine/simplicio_engine.py`](engine/simplicio_engine.py) is the native Simplicio capture engine
(stdlib-only, fail-open) — a **full reimplementation of the upstream
[headroom](https://github.com/headroomlabs-ai/headroom) surface with no external dependency**. Run any
command via the [`scripts/simplicio-engine`](scripts/simplicio-engine) wrapper (e.g. `simplicio-engine doctor`):

| Command | What it does |
|---|---|
| `proxy` | the transparent capture proxy — routes each model to its **real** provider, compresses + measures + caches (no model swap) |
| `doctor` | proxy reachability + lifetime savings |
| `cache` | native response cache (`stats`/`clear`) — a repeated deterministic request is served from cache, skipping the LLM call |
| `signatures` | signatures-only view of a source file (bodies stripped, ~93% fewer tokens to read code) |
| `semantic` | reversible extractive (semantic-lite) compression |
| `kompress` | **ONNX** semantic token-pruning via the real `kompress-v2-base` model |
| `detect` | content-type detection + smart per-block routing |
| `rag` | TF-IDF (or `--ml` embedding) retrieval over the CCR memory store |
| `memory` | CCR compress-cache-retrieve store (`remember`/`recall`/`forget`/`list`/`stats`) |
| `mcp` | native stdio MCP server (compress / retrieve / stats tools) |
| `init` / `wrap` | register Simplicio into a client (Claude / Codex / Copilot / OpenClaw) · run a client with capture routing |
| `report` / `audit` / `capture` / `evals` | savings report · audit a tree for compression opportunity · dry-run a request · compression regression gate |

### 🧠 Optional real ML models — `pip install "simplicio-loop[onnx]"`

Four **real**, public (Apache-2.0) ONNX models run natively — the same models the upstream uses.
Without the extra, the deterministic stdlib path covers everything; models download on first use.

| Model | Command | Use |
|---|---|---|
| `kompress-v2-base` | `simplicio-cli kompress` | semantic token pruning |
| `technique-router-onnx` | `simplicio-cli router` | technique routing |
| `all-MiniLM-L6-v2-onnx` | `simplicio-cli embed` · `rag --ml` | embeddings + semantic RAG |
| `siglip-image-encoder-onnx` | `simplicio-cli image` | image-compression content verifier |

### ⚙️ Native Rust performance core (optional)

[`rust/`](rust) ships four crates ported + rebranded from the upstream (Apache-2.0; `NOTICE` credits it):
`simplicio-core` (compressors + smart-crusher), `simplicio-py` (PyO3 bindings), `simplicio-proxy`
(axum reverse proxy), `simplicio-parity` (Rust↔Python parity harness). Build with `maturin` — the Python
engine works fully without them; the crates only add native speed.

---

## 🏛️ Design pillars (in detail)

Four mechanisms sustain the orchestration power:

| Pillar | Focus | Lives in |
|---|---|---|
| **DAG + pipeline** | parallelism by dependency, staged per item | `references/orchestration.md` (Step 3 pool + pipeline) |
| **Isolation by worktree** | parallel edits without corrupting the tree, merge-gated | `references/orchestration.md` |
| **Adversarial verify** | panel of skeptics before "delivered" | `references/quality-safety-delivery.md` · skill `simplicio-review` |
| **Loop budget cap** | anti-infinite-loop, dual exit | `references/standing-loop-247.md` · skill `simplicio-loop` |

---

## 🚀 Install & use

```bash
git clone https://github.com/wesleysimplicio/simplicio-loop
cd simplicio-loop

# install for your runtime (omit <runtime> to auto-detect)
bash scripts/install.sh <runtime> [--global] [--minimal]        # macOS / Linux
pwsh scripts/install.ps1 <runtime> [-Global]                    # Windows
# <runtime> ∈ claude codex vscode cursor antigravity kiro opencode gemini aider hermes openclaw
```

**Install is complete by default — it installs everything.** One command sets up the whole stack:
the two loop operators (`simplicio-mapper` + `simplicio-cli`, auto-handling PEP 668 / externally-managed
Python and symlinking the binaries onto `PATH`), the **full Python stack** (the package + the `[onnx]`
models backend: onnxruntime + huggingface_hub + tokenizers + pillow, so `simplicio-cli kompress/router/embed/image`
work), the **6 skills + hooks** with the loop's Stop hook wired, and the **always-on capture proxy**
with Claude + Codex + Hermes **routed and measured** in the background. The **dashboard opens once** on a
fresh install, then it's on-demand (`simplicio-loop dashboard` / `simplicio-economy.sh monitor`); the
**menu-bar tray never opens by itself** — nothing is forced to stay open.
Pass **`--minimal`** only for headless/CI to skip the heavy deps + the machine services. Verify any time:
`bash scripts/simplicio-economy.sh status`.

### Update

```bash
bash scripts/update.sh [<runtime>]    # git pull → reinstall skills/hooks/operators → restart services
```

`update.sh` stashes local edits, fast-forwards `main`, reinstalls from the fresh source, restarts the
launchd/systemd services so they run the new code, and prints the live stack + savings.

### Doctor — verify + repair

```bash
python3 scripts/doctor.py            # report the whole stack (REQUIRED vs OPTIONAL)
python3 scripts/doctor.py --repair   # install/wire what's fixable; make everything operational
# also: bash scripts/simplicio-economy.sh doctor [--repair]
```

`doctor` separates **REQUIRED** (python3, the two loop operators, the 6 skills, the loop hooks, the
capture proxy — `--repair` installs/wires them) from **OPTIONAL** accelerators (the ONNX models
backend, the native **Rust** core, the tray dep). **Missing an optional piece is never a failure and
never blocks** — the Python engine + the deterministic path cover everything; the exit code is 0 as
long as every REQUIRED item is healthy.

Or, on Claude Code / Cursor, install it straight from the latest GitHub release (no marketplace):

```bash
gh release download --repo wesleysimplicio/simplicio-loop --archive tar.gz
tar xzf simplicio-loop-*.tar.gz && cd simplicio-loop-*/
bash scripts/install.sh claude    # or: bash scripts/install.sh cursor
```

Then:

```
/simplicio-tasks finish all the open issues
```

The only requirement is **python3** on PATH (skills, hooks, and installer are cross-platform
Python). For GitHub sources, `git` + an authenticated `gh`. See [`INSTALL.md`](INSTALL.md) and
[`adapters/MATRIX.md`](adapters/MATRIX.md).

**Before an unattended 24/7 run:** set a cost ceiling in `.orchestrator/loop-budget.json`
(`daily_usd_ceiling > 0`), confirm source auth is persistent, and keep the irreversible-op human
gate + secret-scan on. With `ceiling = 0` the watcher refuses to run unattended (fail-safe).

---

## 🔒 Safety (non-negotiable)

- **Secret-scan** every diff; block on hit.
- **Irreversible-op human gate** — force-push, history rewrite, prod deploy, data/schema delete,
  mass-file delete → stop and ask. Headless + no approver → remove the destructive capability.
- **Enforced, not just promised** — `hooks/action_gate.py` is a **fail-closed** `PreToolUse` /
  git-pre-push hook that mechanically blocks the above (and secret-laden commits) *before* they run.
  The safety contract holds even if the model forgets it. `selftest` proves the ruleset (15/15).
- **4-state pre-execution verdict** — optimization may never raise a command's risk tier.
- **Trust-before-load** — perception-shaping config (clamp profiles, suppression lists) is
  untrusted until a human reviews and hash-pins it.
- **Prompt-injection hardening** — item/PR/comment content can never override the contract.
- **Hard $ kill-switch** for unattended runs; **evidence-gated** completion (never a false
  "done"); **fail-open** hooks (never trap the agent in a loop).

---

## ✅ Tests & local checks (no paid CI)

Claims are verified, not just asserted — and the gate runs **locally**, with zero CI cost:

```bash
python3 scripts/check.py            # the whole gate (audit + tests)
```

- **Test suite** (`tests/`) — the workers' deterministic `selftest`s, plus an **e2e of the loop
  driver** (`hooks/loop_stop.py`): it proves the loop **stops on evidence**, **ignores a bare
  `<promise>`**, and **stops on the cap** as distinct exits — and that the evidence producers
  **BLOCK** (never fake-pass) when their toolchain is absent. Runs under `pytest` *or*, with no pip
  at all, self-runs on bare python3 (`python3 tests/test_*.py`).
- **Claims audit** (`scripts/claims_audit.py`, fail-closed) — every `scripts/*.py` the docs
  reference exists · the extension-point count agrees across all files · each cited worker command
  actually runs · the shipped `simplicio_loop/_bundle/` skills are **byte-identical** to source.
- **Wire it as a git pre-push hook** to keep `main` honest for free:
  ```bash
  printf '#!/bin/sh\npython3 scripts/check.py\n' > .git/hooks/pre-push && chmod +x .git/hooks/pre-push
  ```

`pip install "simplicio-loop[dev]"` adds pytest for nicer output; it is never required.

---

## 📄 License

MIT
