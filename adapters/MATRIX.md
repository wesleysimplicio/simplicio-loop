# Runtime adapter matrix — simplicio-tasks super-plugin

One universal skill core (`.claude/skills/`, 6 skills) + one set of hooks (`hooks/`) drives
**every** runtime. An adapter is thin: it tells a runtime *where to load the skills*, *how to
arm the loop*, and *how to bind native speed*. Nothing in the protocol is runtime-specific —
this is the inverted dependency (the skill names no runtime; the runtime detects the skill).

Three capabilities decide how rich an adapter is:

- **Skill load** — how the runtime discovers `SKILL.md` files.
- **Loop drive** — how `simplicio-loop` re-feeds the goal: a real **stop-hook**, or the
  **self-paced** fallback (host scheduler / cron / `/loop`).
- **Native bind** — whether `simplicio-runtime` (or a native command set) binds the extension
  points for near-zero-token determinism; otherwise the LLM fallbacks cover 100%.

`orient_clamp.py` (token economy) works on **all** runtimes with no wiring — it's just a wrapper.

| # | Runtime | Skill load | Loop drive | Hooks | Native bind | Adapter |
|---|---|---|---|---|---|---|
| 1 | **Claude Code** | `.claude/skills/` + `.claude-plugin/` | `Stop` hook | ✅ full | MCP (`simplicio-cli mcp register`) | [claude](claude/README.md) |
| 2 | **Codex** | `AGENTS.md` → `SKILL.md` | self-paced | ⚠️ partial | MCP / Python adapter | [codex](codex/README.md) |
| 3 | **VS Code (Copilot)** | `.github/copilot-instructions.md` | self-paced (tasks) | ⚠️ tasks | MCP (VS Code MCP) | [vscode](vscode/README.md) |
| 4 | **Cursor** | `.cursor-plugin/` + `.claude/skills/` | `stop` + `afterAgentResponse` | ✅ full | MCP / rules | [cursor](cursor/README.md) |
| 5 | **Antigravity** | rules / `AGENTS.md` | self-paced | ⚠️ | MCP | [antigravity](antigravity/README.md) |
| 6 | **Kiro** | `.kiro/steering/` | self-paced (specs) | ⚠️ | MCP | [kiro](kiro/README.md) |
| 7 | **OpenCode** | `AGENTS.md` + config | self-paced | ⚠️ | MCP | [opencode](opencode/README.md) |
| 8 | **Gemini** | `GEMINI.md` → `SKILL.md` | self-paced | ⚠️ | MCP / native adapter | [gemini](gemini/README.md) |
| 9 | **Aider** | `CONVENTIONS.md` (read) | self-paced | ❌ | — (LLM fallback) | [aider](aider/README.md) |
| 10 | **Hermes** | native skill recall | native loop | ✅ native | **native** (extension points) | [hermes](hermes/README.md) |
| 11 | **OpenClaw** | plugin SDK / `skills/` | native scheduler | ✅ native | **native** (plugin SDK) | [openclaw](openclaw/README.md) |

Legend: ✅ first-class · ⚠️ partial / via a generic mechanism · ❌ none (degrade to fallback).

## Install (any runtime)

```bash
# from a clone of this repo:
bash scripts/install.sh <runtime> [--global]      # macOS/Linux
pwsh scripts/install.ps1 <runtime> [-Global]      # Windows / pwsh
# <runtime> ∈ claude codex vscode cursor antigravity kiro opencode gemini aider hermes openclaw
# omit <runtime> to auto-detect
```

The installer copies the 6 skills into the runtime's skills location, wires the loop hooks
where supported, and prints the MCP-register line for native binding. Everything it does is a
copy + a config edit — reversible, no build.

## What degrades gracefully

- **No stop-hook** → the loop self-paces via the host scheduler (`simplicio-loop` "No-hook
  fallback"). Same exit conditions (evidence-gated promise, cap, budget).
- **No native bind** → the LLM performs every extension point with shell/git/gh/file tools.
- **No skill loader** (e.g. Aider) → the adapter inlines `SKILL.md` as the runtime's
  conventions/instructions file. Larger context, identical behavior.

The promise: **same protocol, same gates, same safety on all 11 — only the speed differs.**

## Verifying an adapter

The installer's contract (skills copied · entry file marked · hooks present/wired) is verified
end-to-end per runtime by `scripts/verify_adapters.py`, which installs into a throwaway target and
asserts each promise — no risk to your real config, runnable in CI:

```bash
python3 scripts/verify_adapters.py                 # all 11
python3 scripts/verify_adapters.py antigravity kiro opencode aider   # a subset
```

That covers everything up to launching the runtime itself. The final manual smoke — open the
runtime, run `/simplicio-tasks <small task>`, confirm the loop drives and the gates fire — is the
one step a file-level harness can't do; do it once per runtime per the adapter's README.
