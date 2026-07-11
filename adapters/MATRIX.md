# Runtime adapter matrix — simplicio-loop super-plugin

One universal skill core (`.claude/skills/`, 6 skills) + one set of hooks (`hooks/`) drives
**every** runtime. An adapter is thin: it tells a runtime *where to load the skills*, *how to
arm the loop*, and *how to bind native speed*. Nothing in the protocol is runtime-specific —
this is the inverted dependency (the skill names no runtime; the runtime detects the skill).

Three capabilities decide how rich an adapter is:

- **Skill load** — how the runtime discovers `SKILL.md` files.
- **Loop drive** — how `simplicio-loop` re-feeds the goal: a real **stop-hook**, or the
  **self-paced** fallback (host scheduler / cron / `/loop`).
- **Native bind** — whether `simplicio-runtime` (or a native command set) binds the extension
  points for near-zero-token determinism. **REQUIRED, not optional, on Claude, Codex, Cursor,
  VS Code, Antigravity, Kiro, OpenCode, and Hermes** (`scripts/install_lib.py`
  `FORCED_BIND_RUNTIMES`) — these 8 must verify the bind (`simplicio doctor --json`) and STOP
  rather than silently run the unbound LLM fallback. Gemini, Aider, and OpenClaw keep the bind
  optional/native-by-design per their own adapter.

`orient_clamp.py` (token economy) works on **all** runtimes with no wiring — it's just a wrapper.

## Runtime tiers

Maintaining real parity across 11 distinct runtimes is infeasible — each host changes its hook/skill
format every release. This repo therefore adopts a **two-tier system**:

### Tier 1 — Guaranteed (gated)

Three runtimes are **verified mechanically on every commit** and enjoy real parity:

| # | Runtime | Skill load | Loop drive | Hooks | Native bind | Adapter |
|---|---|---|---|---|---|---|
| 1 | **Claude Code** | `.claude/skills/` + `.claude-plugin/` | `Stop` hook | ✅ full | MCP, **REQUIRED** (`simplicio install --global`) | [claude](claude/README.md) |
| 2 | **Codex** | `AGENTS.md` → `SKILL.md` | self-paced | ⚠️ partial | MCP / Python adapter, **REQUIRED** | [codex](codex/README.md) |
| 3 | **Cursor** | `.cursor-plugin/` + `.claude/skills/` | `stop` + `afterAgentResponse` | ✅ full | MCP / rules, **REQUIRED** | [cursor](cursor/README.md) |

These three are covered by:
- `scripts/verify_adapters.py` running against each tier-1 runtime's install contract
- Gate check `adapter-install-contract` in `scripts/claims_audit.py` (fast per-runtime verification)
- `FORCED_BIND_RUNTIMES` as enforced project policy

**To enter Tier 1**, a runtime must:
1. Have an adapter with documented skill-load, loop-drive, hooks, and native-bind columns
2. Pass `scripts/verify_adapters.py <runtime>` (idempotent, throwaway target, zero risk to real config)
3. Maintain a passing gate for 1 full release cycle without a regression

**To exit Tier 1** (demotion to Tier 2), a runtime:
1. Fails `verify_adapters.py` for 2 consecutive releases, or
2. The upstream runtime changes its skill/hook format and no PR adapts within 1 release cycle

### Tier 2 — Best-effort (ungated)

Eight runtimes are documented and supported on a best-effort basis — contributions welcome,
no gate, no parity promise per release:

| # | Runtime | Skill load | Loop drive | Hooks | Native bind | Adapter |
|---|---|---|---|---|---|---|
| 4 | **VS Code (Copilot)** | `.github/copilot-instructions.md` | self-paced (tasks) | ⚠️ tasks | MCP, **REQUIRED** | [vscode](vscode/README.md) |
| 5 | **Antigravity** | rules / `AGENTS.md` | self-paced | ⚠️ | MCP, **REQUIRED** | [antigravity](antigravity/README.md) |
| 6 | **Kiro** | `.kiro/steering/` | self-paced (specs) | ⚠️ | MCP, **REQUIRED** | [kiro](kiro/README.md) |
| 7 | **OpenCode** | `AGENTS.md` + config | self-paced | ⚠️ | MCP, **REQUIRED** | [opencode](opencode/README.md) |
| 8 | **Gemini** | `GEMINI.md` → `SKILL.md` | self-paced | ⚠️ | MCP / native adapter (optional) | [gemini](gemini/README.md) |
| 9 | **Aider** | `CONVENTIONS.md` (read) | self-paced | ❌ | — (LLM fallback, no bind exists) | [aider](aider/README.md) |
| 10 | **Hermes** | native skill recall | native loop | ✅ native | **native** (extension points), **REQUIRED** | [hermes](hermes/README.md) |
| 11 | **OpenClaw** | plugin SDK / `skills/` | native scheduler | ✅ native | **native** (plugin SDK) | [openclaw](openclaw/README.md) |

Legend: ✅ first-class · ⚠️ partial / via a generic mechanism · ❌ none (degrade to fallback).
**REQUIRED** = native bind is mandatory project policy on this host (rows 1–7 + 10); not following
it is a policy violation, not a graceful degradation — see `scripts/install_lib.py`
`FORCED_BIND_RUNTIMES` and each adapter's "Native bind (REQUIRED)" section.

## Install (any runtime)

```bash
# from a clone of this repo:
bash scripts/install.sh <runtime> [--global]      # macOS/Linux
pwsh scripts/install.ps1 <runtime> [-Global]      # Windows / pwsh
# <runtime> ∈ claude codex vscode cursor antigravity kiro opencode gemini aider hermes openclaw
# omit <runtime> to auto-detect
```

The installer copies the 6 skills into the runtime's skills location, wires the loop hooks
where supported, and — on the 8 `FORCED_BIND_RUNTIMES` (claude, codex, cursor, vscode,
antigravity, kiro, opencode, hermes) — actually applies the native MCP/CLI bind
(`ensure_runtime_bind` in `scripts/install_lib.py`), not just prints a suggestion. Everything it
does is a copy + a config edit — reversible, no build.

## Loop→Runtime contract adapter

All native bindings use the transport-neutral [`docs/runtime-adapter.md`](../docs/runtime-adapter.md)
contract. The adapter negotiates `simplicio.runtime/v1`, preserves the same Run/WorkItem IDs,
buffers operations during outages, and fails closed on incompatible versions. Standalone mode is
available only with an explicit `standalone=True` choice and never claims runtime delivery.

## What degrades gracefully — and what does not

- **No stop-hook** → the loop self-paces via the host scheduler (`simplicio-loop` "No-hook
  fallback"). Same exit conditions (evidence-gated promise, cap, STOP). This degradation is
  always allowed — it's a drive-mechanism choice, not a policy violation.
- **No native bind, on Tier 2 only (Gemini/Aider/OpenClaw)** → the LLM performs every extension point
  with shell/git/gh/file tools. This is the one allowed bind-fallback; it does NOT apply to the
  8 `FORCED_BIND_RUNTIMES` (see above) — there, an unreachable bind is a STOP-and-report
  condition, not a silent degrade.
- **No skill loader** (e.g. Aider) → the adapter inlines `SKILL.md` as the runtime's
  conventions/instructions file. Larger context, identical behavior.

The promise: **same protocol, same gates, same safety on all 11 — Tier 1 verified mechanically,
Tier 2 best-effort with contributions welcome.**

## Verifying an adapter

The installer's contract (skills copied · entry file marked · hooks present/wired) is verified
end-to-end per runtime by `scripts/verify_adapters.py`, which installs into a throwaway target and
asserts each promise — no risk to your real config:

```bash
python3 scripts/verify_adapters.py tier1                        # Tier 1 — gated, run on every commit
python3 scripts/verify_adapters.py claude codex cursor          # same as above
python3 scripts/verify_adapters.py                              # all 11 (~45s/runtime — run manually or in a slower CI job)
python3 scripts/verify_adapters.py antigravity kiro opencode aider   # a Tier-2 subset
```

`scripts/claims_audit.py` (check 7, part of `python3 scripts/check.py`) runs the fast, single-runtime
form (`verify_adapters.py claude`, ~15s) on every gate so the Tier-1 install contract is never dead
assurance — it does NOT run the full 11-runtime sweep above; run that manually before a release.

That covers everything up to launching the runtime itself. The final manual smoke — open the
runtime, run `/simplicio-loop <small task>`, confirm the loop drives and the gates fire — is the
one step a file-level harness can't do; do it once per runtime per the adapter's README.
