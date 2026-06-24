# 💳 Pricing & Monetization — simplicio-loop

> **Status: proposal (draft).** This document sketches a paid offering on top of the
> open-source simplicio-loop. It is a business proposal for the maintainer to decide on — the
> core stays free and open. Nothing here is billed today.

## Model — Open-core + usage-based hosted tier

The **engine stays MIT and free**: the `/simplicio-tasks` orchestrator, the five satellite
skills, the hooks, the 44 extension points, and the token-economy stack. Adoption is the moat —
the free tier must be fully usable, self-hostable, and never crippled.

The **paid offering is convenience + scale**, not the code:

| Tier | Price (suggested) | Who | What you get beyond the free core |
|---|---|---|---|
| **Community** | **Free, MIT** | individuals, OSS | Full engine self-hosted. All skills, hooks, local token-economy dashboard, local video_evidence (hyperframes local render). No limits you didn't set yourself. |
| **Pro (hosted)** | **~$29 / dev / mo** | small teams | Managed 24/7 watcher (survives reboot, we run it), hosted token-economy dashboard with **history + savings reports**, the `simplicio-mapper` / `simplicio-dev-cli` operators as a service, priority model-routing. Soft usage cap. |
| **Team / Cloud** | **usage-based** (see below) | orgs | Everything in Pro + multi-repo fleets, SSO, audit log, distributed `video_evidence` render (hyperframes on AWS Lambda), per-seat + per-run metering, SLA. |
| **Enterprise** | **custom** | regulated | Self-hosted control plane, on-prem operators, commercial license/indemnity, support. |

Why open-core (vs. dual-license or pure-SaaS): the skill's whole thesis is *"the skill names no
runtime; the runtime detects the skill"* — portability is the product. Gating the engine would
break that. We charge for **running it for you** (the watcher, the operators, the dashboard, the
fleet), which is exactly the part that costs us compute.

### What is free forever (never gated)
- The orchestrator loop + all 6 skills + hooks.
- Local token economy: `orient_clamp.py`, the capture engine, `savings_ledger` on disk, the
  local dashboard at `http://127.0.0.1:9090`.
- `video_evidence` **local** render (hyperframes via local Node 22+/FFmpeg).
- Self-hosting on any of the 11 runtimes.

### What the paid tiers add (convenience + scale, not capability lock-in)
- **We host the 24/7 watcher** so it survives reboots without the user running their own cron.
- **Hosted operators** — `simplicio-mapper` + `simplicio-dev-cli` as a managed service (no local install).
- **Savings history + team dashboard** — the local one-session view becomes a retained,
  multi-repo, multi-seat analytics surface.
- **Distributed video render** — `video_evidence` offloaded to hyperframes/AWS Lambda for big
  walkthroughs (local render stays free).

---

## 💰 Billing architecture — sketch

The good news: the metering primitives **already exist** in the repo. Billing is mostly a matter
of aggregating what the loop already records, never a new measurement layer.

### 1. The two meters we already produce

| Primitive | File / source | What it gives billing |
|---|---|---|
| **Cost kill-switch** | `.orchestrator/loop-budget.json` | per-run / per-day USD ceiling, `spent_usd_today`, `reset_at`, `state` — the spend ledger the loop already enforces |
| **Savings ledger** | `savings_ledger` ext-point · `scripts/savings_harness.py` · `proxy_savings.json` | REAL token spend per session (snapshot→score, fixed `ceil(chars/4)` tokenizer) + tokens saved |
| **Capture proxy** | `engine/simplicio_engine.py` (`proxy`) · `scripts/simplicio-economy.sh` | actual upstream token usage per provider call (already intercepted) |
| **Run outcomes** | `trajectory` ext-point | per-run records → unit of "a delivered item" for per-run pricing |

```text
loop turn ──> capture proxy (real upstream tokens)
          ──> savings_ledger (spent + saved, deterministic score)
          ──> loop-budget.json (USD spend vs ceiling, kill-switch)
                         │
                         ▼
              billing aggregator (NEW, thin)
                         │
          ┌──────────────┼───────────────┐
          ▼              ▼                ▼
   per-seat (Pro)  per-run (Team)   metered usage (Cloud)
```

### 2. Three meters → three price levers

- **Per-seat (Pro):** flat; the aggregator only checks an active-seat count. No new metering.
- **Per-run (Team):** one `trajectory` record = one billable "delivered item". Count
  closed/merged items from the source re-query the loop already does at delivery (Step 6).
- **Metered usage (Cloud):** sum `spent_usd_today` across the org's runs from `loop-budget.json`,
  plus managed-operator minutes and Lambda render seconds. The kill-switch doubles as the
  **prepaid-credit guard** — when credits hit zero, `state: "halted"` already stops the loop
  cleanly (fail-safe), so we never over-serve.

### 3. The aggregator (the only new component)

A thin, **deterministic, model-free** service — same discipline as `savings_harness score`:

```
scripts/billing_aggregator.py   (proposed)
  collect   read .orchestrator/{loop-budget.json, savings/snapshots.jsonl, trajectory/*} for a window
  meter     roll up: usd_spent, tokens_in/out, tokens_saved, items_delivered, render_seconds
  invoice   apply the tier rule (seat | run | metered) → a line-item JSON (no model call)
  export    emit to Stripe metered billing / a CSV — usage records only, never customer code
```

Properties (non-negotiable, mirror the existing safety spine):
- **Deterministic & offline** — billing math never calls a model (reproducible from on-disk
  records, exactly like `savings_harness score`).
- **Privacy** — only usage *counts* leave the box (tokens, USD, item-ids, seconds); **never**
  customer code, diffs, prompts, or the captured screenshots/MP4s.
- **Fail-safe** — the prepaid guard reuses the kill-switch: zero balance ⇒ `state: "halted"` ⇒
  loop stops. No silent overage.
- **Auditable** — every invoice line traces to an immutable snapshot record (the same audit
  property the loop requires of its `<promise>` evidence).

### 4. Suggested metered rates (placeholder — tune against real proxy data)

| Meter | Unit | Suggested |
|---|---|---|
| Managed run | per delivered+merged item | $0.50–$2.00 |
| Tokens (passthrough) | per 1M upstream tokens | cost + 15% |
| Distributed video render | per render-minute (Lambda) | $0.10 |
| Managed operator time | per compute-minute | $0.02 |

> Rates are illustrative. The capture proxy already records real per-provider token usage, so the
> first month of `proxy_savings.json` data should set these empirically before any public price.

---

## Open questions for the maintainer

1. **License:** keep pure MIT (open-core, charge for hosting) — recommended — or move to a
   dual-license for the operators? Open-core needs **no** license change; dual-license does.
2. **Billing rail:** Stripe metered billing is the lowest-lift target for the aggregator's
   `export`. Confirm before wiring.
3. **Where the control plane lives:** fully managed by us vs. a self-hosted control plane for
   Enterprise (changes the aggregator's deployment, not its logic).

*Next step if approved:* implement `scripts/billing_aggregator.py` (collect/meter/invoice/export)
against the existing `.orchestrator/` records, with a `selftest` like `savings_harness` so the
billing math is provably deterministic.
