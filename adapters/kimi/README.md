# Kimi adapter

Kimi (Moonshot AI) is reachable as a coding agent mainly through **Kimi CLI** and through
OpenAI-compatible API wrappers used by third-party agent shells. It has no first-party,
independently-documented MCP client contract as stable as Claude/Cursor/VS Code's — this adapter
is intentionally **best-effort**.

## Install

`kimi` is not yet a recognized target of `scripts/install.sh`/`scripts/install_lib.py` (see
`adapters/MATRIX.md` § Install for the currently wired runtime list). Until it is wired in,
install by hand, mirroring the Aider pattern since Kimi has no native `.claude/skills`-style skill
loader:

```bash
cp .claude/skills/simplicio-loop/SKILL.md CONVENTIONS.md   # or your Kimi shell's own instructions file
```

## Loop drive — self-paced

No stop-hook. Drive ticks on a schedule via whichever CLI entrypoint your Kimi install exposes,
same exit conditions (evidence-gated promise, cap, spindle handoff, STOP) as every other
self-paced runtime.

## Token economy

`orient_clamp.py` works as-is: `python3 hooks/orient_clamp.py -- <build/test/diff command>`.

## Native bind — MCP (optional, best-effort wiring)

`simplicio-runtime` native binding is optional on Kimi. A missing/unreachable bind reports
explicit degraded mode while the standalone loop remains available. What differs on Kimi is how
well documented and verified the wiring is, not whether the
bind is optional.

```bash
pip install -U simplicio-installer && simplicio install --global
```

## MCP config

- **Config file:** **not independently verified.** Kimi CLI's MCP support (where present) tracks
  the same `mcpServers`-keyed JSON shape used by most Claude/Anthropic-compatible tooling, and
  some Kimi integrations run entirely through an OpenAI-compatible wrapper/agent shell that has
  its own separate MCP config (e.g. an OpenCode or Claude Code front-end pointed at a Kimi model).
  If you're routing through such a wrapper, use that wrapper's own adapter section in this
  matrix (e.g. [opencode](../opencode/README.md#mcp-config)) instead of this file.
- **Best-effort snippet** (mirrors the common `mcpServers` shape; confirm against your actual
  Kimi CLI version's docs before relying on it):

```json
{
  "mcpServers": {
    "simplicio": {
      "command": "simplicio",
      "args": ["serve", "--mcp", "--stdio"],
      "cwd": "/path/to/your/repo"
    }
  }
}
```

- **Verify:** `simplicio doctor --json | grep -A2 mcp-host-registration` confirms the runtime
  side (binary reachable, contracts smoke passing); there is no verified Kimi-side MCP status
  command known to this repo. Tier: **best-effort / community-reported, not gated** — treat any
  claim of a fully working Kimi MCP integration as unverified until confirmed against a real
  install.

## Use

Whatever prompt surface your Kimi CLI/agent shell exposes: paste or reference the goal, e.g.
"`/simplicio-loop finish all the open issues`" if the shell loads the inlined conventions file.

## Progresso do run

Self-paced (N2): the tick should echo `python3 scripts/loop_progress.py render --turn-header`.
Universal fallback (N3): open `.orchestrator/loop/PROGRESS.md` (auto-regenerated every turn) —
this works with zero Kimi-specific code.
