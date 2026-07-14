# Cursor adapter

First-class: native plugin manifest (`.cursor-plugin/`), `stop` + `afterAgentResponse` hooks,
rules, and MCP. This repo IS a valid Cursor plugin.

## Install

```bash
bash scripts/install.sh cursor
```

Or add the marketplace and install:

```
# Cursor → Settings → Plugins → Add from Git: wesleysimplicio/simplicio-loop
```

The root `.cursor-plugin/plugin.json` declares the skills (`./.claude/skills/`) and hooks
(`./hooks/hooks.json`). `hooks/hooks.json` is already in Cursor's format.

## Loop drive — two-hook split (the original Ralph pattern)

`hooks/hooks.json` wires:
- `afterAgentResponse` → `loop_capture.py` (raise `done` on an evidence-backed `<promise>`)
- `stop` → `loop_stop.py` (re-feed the goal, or exit on promise/cap/STOP)

Detection and termination are decoupled — neither parses the other's state inline.

## Token economy

`orient_clamp.py` works as-is. For automatic clamping, add a `beforeShellExecution`-style
rewrite in your Cursor hooks pointing at `orient_rewrite.py` (opt-in; conservative + fail-open).

## Native bind — MCP / rules (optional)

`simplicio-runtime` native binding is optional on Cursor. Install it by hand when you want MCP
or model-router capabilities:

```bash
pip install -U simplicio-installer && simplicio install --global   # registers Cursor's MCP config
```

Use `simplicio doctor --json` to diagnose an installed bind. A `.cursor/rules/` entry can pin
model-per-role choices (pstack-style) if you use the simplicio-runtime model router.

## Use

```
/simplicio-tasks finish all the open issues
```
