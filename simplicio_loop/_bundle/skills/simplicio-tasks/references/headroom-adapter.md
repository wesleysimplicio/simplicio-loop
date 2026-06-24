# Headroom adapter — context compression proxy + MCP accelerator

Binds to the **`compress`**, **`recall`**, and **`learn`** extension points by providing a
transparent compression proxy and MCP server for deterministic context reduction.

**Headroom** ([github.com/chopratejas/headroom](https://github.com/chopratejas/headroom)) is a
context compression layer that compresses tool outputs, logs, RAG chunks, files, and conversation
history before they reach the LLM — 60-95% fewer tokens with no quality loss.

## What it provides

| Capability | Extension point | Benefit |
|---|---|---|
| Proxy server (`headroom proxy --port 8787`) | `compress` | Transparent compression of ALL tool output — no per-command wrapping needed |
| MCP server (`headroom_compress`, `headroom_retrieve`, `headroom_stats`) | `compress` + `recall` | Deterministic compression + retrieval + real-time token stats |
| 6 compression algorithms (CacheAligner, ContentRouter, SmartCrusher, CodeCompressor, Kompress-base) | `compress` | Auto-detect content type, pick best compressor |
| Cross-agent memory store | `recall` | Shared store across Claude Code, Codex, Gemini, and Hermes |
| `headroom learn` — mines failed sessions → writes corrections | `learn` | Automated failure mining, no manual retrospective |

## Comparison with existing simplicio-loop compression

| Capability | Current (simplicio-loop) | Headroom addition |
|---|---|---|
| Output clamping | `orient_clamp.py` (per-command wrapper) | `headroom proxy` (daemon, transparent, no wrapping) |
| Compression algorithms | `simplicio-compress` (prose levels only) | 6 algorithms (JSON AST, text, semantic) |
| Token monitoring | `savings_ledger` (per-session summary) | `headroom_stats` (real-time via MCP) |
| Cross-agent memory | `simplicio-learn` (per-repo, post-run) | Shared store across agent types, real-time |
| Learn from failures | Manual retrospective | `headroom learn` (automated, writes to CLAUDE.md) |

## How it works

```
 Your agent / app
   (simplicio-loop, Claude Code, Hermes, etc.)
        │   prompts · tool outputs · logs
        ▼
    ┌─────────────────────────────────────────┐
    │  headroom proxy --port 8787             │
    │  (runs locally, data stays local)       │
    │  ─────────────────────────────────────  │
    │  CacheAligner  →  ContentRouter  →  CCR │
    │                    ├─ SmartCrusher      │
    │                    ├─ CodeCompressor    │
    │                    └─ Kompress-base     │
    └─────────────────────────────────────────┘
        │   compressed prompt  +  retrieval tool
        ▼
 LLM provider
```

## Installation

```bash
pip install headroom-ai
# Or via npm: npx headroom-ai
```

## Using in the orchestrator flow

### Step 1a — Pre-flight (detect headroom)

```bash
headroom --version 2>/dev/null && echo "headroom available" || echo "pip install headroom-ai"
```

Add to `.orchestrator/loop-budget.json`:
```json
{
  "daily_usd_ceiling": 5.0,
  "headroom": {
    "enabled": true,
    "proxy_port": 8787,
    "estimate_savings_pct": 40
  }
}
```

### Step 1c — Token-economy gate (proxy mode)

Start the headroom proxy before the loop:

```bash
headroom proxy --port 8787 &
```

Then route all shell command output through it automatically. Headroom compresses
tool outputs before the LLM sees them — no per-command wrapping needed.

When the proxy is running, `orient_clamp.py` is still active for tee-on-failure; the
compression layer is additive (headroom compresses → orient_clamp clamps → LLM reads).

### compress extension point — MCP mode

Bind via MCP for deterministic compression:

```bash
# Start headroom MCP server (auto-detected by simplicio-loop)
headroom mcp --port 8787 &
```

The MCP server exposes three tools:
- `headroom_compress(content)` — compress text deterministically
- `headroom_retrieve(key)` — retrieve original from CCR cache
- `headroom_stats()` — real-time token savings statistics

When MCP is bound, the orchestrator delegates ALL compression to headroom instead of
the LLM fallback (summarize to bullets). This is L0 deterministic — zero LLM tokens spent
on compression.

### recall extension point — cross-agent memory

Headroom's shared store persists across sessions and agent types:

```bash
# Write to cross-agent memory
headroom remember "key" "value"

# Read from cross-agent memory
headroom recall "key"

# Auto-dedup: headroom detects duplicate entries across agents
```

When bound, `recall` reads from headroom's shared store instead of grepping ADRs
and git history.

### learn extension point — automated failure mining

```bash
headroom learn --repo .
```

Mines the last session's failures, extracts correction patterns, and writes them
to `CLAUDE.md` / `AGENTS.md`. Works across Claude Code, Codex, Gemini, and Hermes.

When bound, `learn` delegates to headroom instead of manual retrospective.

### Proxied shell execution

When the proxy is running, wrap commands transparently:

```bash
# Before (manual):
python3 hooks/orient_clamp.py -- <command>

# After (automatic, proxy mode):
<command>  # headroom intercepts and compresses output
```

The proxy does NOT require wrapping — it intercepts at the port level. For best results,
run `headroom proxy --port 8787` at loop start and let it run for the entire session.

### MCP-based command compression

For precise control, use the MCP compress tool from within the loop:

```bash
# Instead of running a command and compressing its output manually:
response=$(run_some_command)
compressed=$(headroom_compress "$response")

# The LLM reads the compressed version; original is retrievable via headroom_retrieve
```

## Token economy

| Technique | Without headroom | With headroom | Savings |
|---|---|---|---|
| Tool output compression | `orient_clamp.py` + `simplicio-compress` | 6 algorithms, auto-detected | 60-95% vs 40-60% |
| Cross-agent memory | `simplicio-learn` (per-repo) | Shared store, auto-dedup | Eliminates duplicate learning |
| Learn from failures | Manual retrospective | Automated via `headroom learn` | ~80% less tokens on learning |
| CCR cache | tee-cache (file-based) | Reversible, cross-session | Fewer re-runs |

Headroom is additive to existing simplicio-loop compression — it doesn't replace
`orient_clamp.py` or `simplicio-compress`, it provides an alternative deterministic path
when the proxy is running.

## Prerequisites

- Python 3.8+ with `pip install headroom-ai`
- Proxy mode: network port 8787 free
- MCP mode: MCP client support in the runtime

## Config reference

```bash
# Proxy mode
headroom proxy --port 8787 --host 127.0.0.1

# Agent wrap (alternative to proxy)
headroom wrap claude     # wraps Claude Code
headroom wrap codex      # wraps Codex CLI
headroom wrap hermes     # wraps Hermes agent

# MCP server
headroom mcp --port 8787

# Stats
headroom stats           # show real-time savings

# Learn from failures
headroom learn --repo .
```

## Live setup (macOS)

### launchd service
The headroom proxy runs as a launchd service for automatic startup:

\
### Status dashboard
\❌ headroom proxy — NOT RUNNING
  │ Total Memories: 0                                                            │
  │ Database Size: 56 KB                                                         │
  📊 Output savings: no data yet (seed with headroom learn)
  💰 Savings ledger: 1 events
  🪵 Logs: /Users/wesleysimplicio/.hermes/logs/headroom.log
### Proxy config for Claude Code
\
### Seed baseline savings
\
============================================================
Verbosity — simplicio-loop
Path: /Users/wesleysimplicio/Projetos/ai/simplicio-loop
============================================================
  Sessions: 1  human turns: 1  responses: 1
  Interrupts:  0  (0% of turns)   ← push-back signal
  Fast-skips:  0 / 0 long answers (0% unread)   ← strongest signal
  Echo ratio:  0.0% of output restated context

  Source: heuristic
  Too few human turns to calibrate; defaulting to L2.

  >> Recommended verbosity level: 2 (confidence: low)

  [WROTE] /Users/wesleysimplicio/.headroom/verbosity.json (level 2)
  [WROTE] /Users/wesleysimplicio/.headroom/output_savings.json (baseline: 0 samples, 0 strata)

  The output shaper now uses this level when HEADROOM_OUTPUT_SHAPER=1 and HEADROOM_VERBOSITY_LEVEL is unset.

## References

- Repo: https://github.com/chopratejas/headroom
- Docs: https://headroom-docs.vercel.app/docs
- PyPI: `pip install headroom-ai`
- npm: `npx headroom-ai`
