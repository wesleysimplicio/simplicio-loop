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

## Native bind (optional, near-zero token)

`simplicio-runtime` via MCP is an optional acceleration on Claude Code. The same loop protocol
can run with the standard-tool fallback when it is not installed. To add the bind by hand:

```bash
pip install -U simplicio-installer && simplicio install --global
```

This registers the MCP server (`simplicio serve --mcp --stdio`) for Claude in one pass (plus
Codex/Cursor/VS Code/Kiro if present). Diagnose the optional bind with:

```bash
simplicio doctor --json
```

## Use

```
/simplicio-tasks finish all the open issues
```

## Progresso do run

Hook-bound (N1): `loop_stop.py` injects fase/etapa/item/ACs/% straight into the re-feed header —
no action needed. Universal fallback (N3, works everywhere): open
`.orchestrator/loop/PROGRESS.md` in the editor (auto-regenerated every turn), or
`watch -n5 cat .orchestrator/loop/PROGRESS.md` in a terminal.
