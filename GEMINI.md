# GEMINI.md — simplicio-loop (Gemini / other runtimes)

The **simplicio-loop** skill is runtime-agnostic. Gemini, Codex, Copilot, Grok, or
any local agent can run it from the same source file.

## Load

Point your agent at:

```
.claude/skills/simplicio-loop/SKILL.md
```

The folder name is `.claude/` for convention, but nothing in the skill is
Claude-specific — it uses only shell, git, gh, file edit, and web.

## Use

```
simplicio-loop: finish all the open issues
```

## Current flow for any LLM

Read `llms.txt` → `AGENTS.md` → `.claude/skills/simplicio-loop/SKILL.md`. Check
`simplicio-mapper --help`, `simplicio-fast --help`, and `simplicio-dev-cli --help`; then run
Mapper `scan`, `inspect`, and `handoff` before deciding an AC-scoped change. Use Fast for
bounded snapshot/PlanDAG context, Dev CLI for the edit plus verification, and real tests/gates
for evidence. Runtime/MCP is off by default (`standalone`); do not invoke it unless explicitly
opted in.

The orchestrator delegates, when loaded, to five satellite skills — `simplicio-loop`
(Ralph loop), `simplicio-orient` (terminal-first token economy), `simplicio-review`
(adversarial review), `simplicio-compress` (prose + memory compression), `simplicio-learn`
(retrospective). All live in `.claude/skills/`.

## Install & token economy

```bash
bash scripts/install.sh gemini      # writes .gemini/settings.json MCP + loads the skills
python3 hooks/orient_clamp.py -- <heavy command>   # reduce output before it hits context
```

## Binding

Use the host-independent [bounded delivery policy](.claude/skills/simplicio-loop/SKILL.md#bounded-delivery-policy)
and its [ADR 0008](docs/adr/0008-bounded-delivery-policy.md) for WIP, frozen scope, reviews,
ownership, integration, and release boundaries.

Where your runtime exposes native capabilities (a repo mapper, a deterministic file
writer, a local model fan-out, a durable scheduler), bind them to the matching
extension points in the Step 1b table so the steps become deterministic and
near-zero-token. Otherwise the LLM fallbacks cover 100% of the work.

See [`AGENTS.md`](AGENTS.md) for the full contract and [`adapters/MATRIX.md`](adapters/MATRIX.md)
for all 12 runtimes.
