# Understand Anything adapter — knowledge-graph code orientation

Binds the **`orient`** and **`recall`** extension points to **Understand Anything**
([Egonex-AI/Understand-Anything](https://github.com/Egonex-AI/Understand-Anything)), a
multi-agent pipeline that turns any codebase into an interactive knowledge graph.

Instead of signatures-only reads (`rg`/`git grep`) or the simplicio-mapper project map, the
orchestrator can use Understand Anything's pre-computed knowledge graph
(`.understand-anything/knowledge-graph.json`) for richer, cheaper code orientation.

## What it provides

| Capability | Extension point | LLM fallback replaced |
|---|---|---|
| Structural graph (files, functions, classes, dependencies) | `orient` | signatures-only reads via `rg`/`git grep` |
| Semantic search ("which parts handle auth?") | `recall` | grepping for keywords |
| Guided tours (architecture walkthrough, ordered by dep) | `orient` | reading files in arbitrary order |
| Diff impact analysis (ripple effects before commit) | `validate` | manual code inspection |
| Domain view (business logic mapping) | `orient` | none (purely LLM) |
| Persona-adaptive explanations (junior vs senior) | `recall` | generic LLM explanation |

## Detection

The adapter is active when the file `.understand-anything/knowledge-graph.json` exists at the
repository root. No config needed.

```bash
test -f .understand-anything/knowledge-graph.json && echo "UA available" || echo "run /understand first"
```

## Knowledge graph schema

The graph is a JSON file with the following top-level structure. Query it deterministically
with `jq` — zero LLM tokens.

```json
{
  "nodes": [
    {
      "id": "src/auth/login.ts",
      "type": "file",
      "label": "login.ts",
      "layer": "API",
      "summary": "Handles user login with email/password and OAuth",
      "children": ["src/auth/login.ts:LoginForm", "src/auth/login.ts:validateCredentials"]
    },
    {
      "id": "src/auth/login.ts:LoginForm",
      "type": "component",
      "label": "LoginForm",
      "layer": "UI",
      "summary": "React component for login form with validation"
    }
  ],
  "edges": [
    { "source": "src/auth/login.ts:LoginForm", "target": "src/auth/login.ts:validateCredentials", "relation": "calls" },
    { "source": "src/auth/login.ts", "target": "src/api/auth.ts", "relation": "imports" }
  ],
  "metadata": {
    "projectName": "my-app",
    "analyzedAt": "2026-06-23T15:00:00Z",
    "totalNodes": 1240,
    "totalEdges": 8900,
    "layers": ["API", "Service", "Data", "UI", "Utility"]
  }
}
```

## Using in the orchestrator flow

### Step 2b-2 — Orient the codebase (PRIMARY use)

When `.understand-anything/knowledge-graph.json` exists, use it as the **primary orientation
source** before implementing a work-item:

```bash
# 1. Find which nodes are relevant to the work-item
jq '[.nodes[] | select(.summary | test("auth|login|session"; "i")) | {id, layer, summary}]' .understand-anything/knowledge-graph.json

# 2. Find the dependency chain (what imports what)
jq '[.edges[] | select(.source | test("auth|login"; "i")) | {from: .source, to: .target, rel: .relation}]' .understand-anything/knowledge-graph.json

# 3. Get architectural context — which layer does this belong to?
jq '[.nodes[] | select(.id == "src/auth/login.ts") | .layer]' .understand-anything/knowledge-graph.json

# 4. Get guided tour for the relevant module
jq '[.nodes[] | select(.id | startswith("src/auth/")) | {id, layer, summary}]' .understand-anything/knowledge-graph.json
```

The orchestrator does these queries with `jq` (deterministic, L0, zero tokens) instead of
reading source files with the LLM.

### Step 6b — Diff impact analysis (before commit)

Before committing a change, use Understand Anything's diff analysis to check ripple effects:

```bash
# Run the diff impact command (Requires understand-anything plugin installed)
/understand-diff
```

Or query the knowledge graph for affected nodes:

```bash
# Find nodes related to changed files
git diff --name-only | xargs -I{} jq --arg f "{}" '[.nodes[] | select(.id | startswith($f)) | {id, summary, children}]' .understand-anything/knowledge-graph.json
```

### Step 1b — Extension point binding

When UA is detected, bind `orient` to the knowledge graph:

```text
orient → orient via .understand-anything/knowledge-graph.json (jq queries)
recall → semantic search via jq queries on the graph (or /understand-chat if plugin active)
```

The simplicio-mapper remains the default `orient` binding; UA is a **richer alternative**
when available. Prefer UA when present.

## Prerequisites

- **Understand Anything installed** in the target runtime (Claude Code / Cursor / Codex / etc.)
  ```bash
  # Via Claude Code plugin marketplace
  /plugin marketplace add Egonex-AI/Understand-Anything
  /plugin install understand-anything
  ```
- **At least one `/understand` run** completed in the project (generates the knowledge graph)
  ```bash
  /understand
  ```
- **Node.js 22+** and **pnpm** (for building the plugin, if not using marketplace)
- Understand Anything also supports Hermes: install via `~/.hermes/skills/`

## Token economy

- The knowledge graph is **pre-computed** — querying it with `jq` costs zero LLM tokens (L0)
- Semantic search is a JSON query, not an LLM call
- Guided tours are already in the graph as ordered node lists
- No LLM fallback needed for structural questions about the codebase
- The graph is **incremental** — subsequent `/understand` runs only re-analyze changed files

## Test offline (no plugin needed)

```bash
# Check if knowledge graph exists
test -f .understand-anything/knowledge-graph.json && echo "UA available"

# Query structure (counts only, zero tokens)
jq '{nodes: (.nodes | length), edges: (.edges | length), layers: .metadata.layers}' .understand-anything/knowledge-graph.json

# Search for a module by name
jq '[.nodes[] | select(.label | test("auth|login|session"; "i")) | {id, layer}]' .understand-anything/knowledge-graph.json
```
