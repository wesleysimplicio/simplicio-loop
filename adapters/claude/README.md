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
/plugin install simplicio-tasks@simplicio
```

Or by hand: copy `.claude/skills/simplicio-*` into your repo's `.claude/skills/` (this repo
already has them — its own agents load them with zero setup).

## Loop drive — `Stop` hook

Add to `.claude/settings.json` (the installer does this for you):

```json
{ "hooks": {
  "Stop": [ { "hooks": [
    { "type": "command", "command": "python3 ./hooks/loop_stop.py" },
    { "type": "command", "command": "python3 ./hooks/learn_stop.py" }
  ] } ],
  "PreToolUse": [ { "matcher": "Bash",
    "hooks": [ { "type": "command", "command": "python3 ./hooks/orient_rewrite.py" } ] } ]
} }
```

`loop_stop.py` re-feeds the goal each turn and exits only on an evidence-backed `<promise>`,
the `max_iterations` cap, or the budget kill-switch. `orient_rewrite` (Bash matcher) is opt-in.

## Token economy

`orient_clamp.py` works immediately: `python3 hooks/orient_clamp.py -- cargo test`. The
`PreToolUse` hook makes it automatic for read-only commands.

## Native bind (optional, near-zero token)

If `simplicio-runtime` is installed, register it so the extension points bind natively:

```bash
simplicio mcp register --client claude-code
```

## Use

```
/simplicio-tasks finish all the open issues
```
