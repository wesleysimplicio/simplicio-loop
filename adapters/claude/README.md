# Claude Code adapter

First-class: native skills, plugin manifest, `Stop`/`PreToolUse` hooks, and MCP binding.

## Install

```bash
bash scripts/install.sh claude            # project-local
bash scripts/install.sh claude --global   # all projects (~/.claude/skills)
```

Or as a marketplace plugin:

```
/plugin marketplace add wesleysimplicio/simplicio-loop
/plugin install simplicio-loop@simplicio
```

Or by hand: copy `.claude/skills/simplicio-*` into your repo's `.claude/skills/` (this repo
already has them — its own agents load them with zero setup).

## Loop drive — `Stop` hook

Add to `.claude/settings.json` (the installer does this for you):

```json
{ "hooks": {
  "Stop": [ { "hooks": [
    { "type": "command", "command": "python3 ./hooks/loop_stop.py" }
  ] } ],
  "PreToolUse": [ { "matcher": "Bash",
    "hooks": [ { "type": "command", "command": "python3 ./hooks/orient_rewrite.py" } ] } ]
} }
```

`loop_stop.py` re-feeds the goal each turn and exits only on an evidence-backed `<promise>`,
the `max_iterations` cap, spindle handoff, or explicit STOP. `orient_rewrite` (Bash matcher) is opt-in.

## Token economy

`orient_clamp.py` works immediately: `python3 hooks/orient_clamp.py -- go test ./...`. The
`PreToolUse` hook makes it automatic for read-only commands.

## Native bind (REQUIRED, near-zero token)

`simplicio-runtime` native bind via MCP is REQUIRED on Claude Code, not optional — every
`simplicio-tasks`/`simplicio-loop`/`simplicio-review` directive must run bound, never silently
degrade to the LLM-only fallback. `scripts/install.sh claude` forces this automatically; by hand:

```bash
pip install -U simplicio-installer && simplicio install --global
```

This registers the MCP server (`simplicio serve --mcp --stdio`) for Claude in one pass (plus
Codex/Cursor/VS Code/Kiro if present). Verify before relying on the orchestrator:

```bash
simplicio doctor --json   # must report the runtime reachable
```

## Use

```
/simplicio-tasks finish all the open issues
```
