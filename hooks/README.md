# Hooks — simplicio-tasks super-plugin

Cross-platform (pure **Python 3**, so identical on Windows / macOS / Linux), and
**fail-open**: a hook that errors or is unsure always lets the agent stop and the command
run unchanged — it can never trap you in a loop or break a command. The real guards are the
`max_iterations` cap and the `$` budget kill-switch, not hook cleverness.

| File | Role | Event |
|---|---|---|
| `loop_stop.py` | simplicio-loop: re-feed the goal or exit (evidence-gated promise + cap + budget) | `stop` / Claude `Stop` |
| `loop_capture.py` | simplicio-loop: raise the `done` flag when an evidence-backed `<promise>` is seen | Cursor `afterAgentResponse` |
| `orient_clamp.py` | simplicio-orient: **wrapper** — run a command, return reduced output + tee-on-failure | called directly, any runtime |
| `orient_rewrite.py` | simplicio-orient: auto-route heavy read-only commands through the clamp (opt-in) | `PreToolUse` |
| `learn_stop.py` | simplicio-learn: queue the finished run for a retrospective | `stop` / `SubagentStop` |

## The always-works one (no wiring needed)

`orient_clamp.py` is a plain wrapper — use it anywhere, any runtime, no hooks:

```bash
python3 hooks/orient_clamp.py -- cargo test          # reduced output, tee log on failure
python3 hooks/orient_clamp.py --json -- git diff      # machine summary
```

Config (optional) `.orchestrator/orient.toml`:

```toml
[tee]   mode = "failures"   # failures | always | never
[hooks] exclude_commands = ["curl", "wget", "playwright", "ssh", "vim", "less"]
```

## Wiring per runtime

### Cursor
`hooks/hooks.json` is already in Cursor's format — the plugin loads it automatically. It wires
the loop (`afterAgentResponse` + `stop`) and the learn trigger.

### Claude Code
Claude uses `settings.json` (project `.claude/settings.json` or user `~/.claude/settings.json`).
Add (paths relative to the repo root, or absolute):

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [
        { "type": "command", "command": "python3 ./hooks/loop_stop.py" },
        { "type": "command", "command": "python3 ./hooks/learn_stop.py" }
      ] }
    ],
    "PreToolUse": [
      { "matcher": "Bash",
        "hooks": [ { "type": "command", "command": "python3 ./hooks/orient_rewrite.py" } ] }
    ]
  }
}
```

`orient_rewrite` is opt-in (the `PreToolUse` block). Omit it to keep clamping manual via
`orient_clamp.py`. Claude has no `afterAgentResponse`; `loop_stop.py` folds capture in by
reading the transcript, so `loop_capture.py` isn't needed there.

### Other runtimes (Codex, Gemini, Aider, OpenCode, Kiro, Antigravity, Hermes, OpenClaw)
Most don't expose a stop hook. Use the **no-hook fallback**: the `simplicio-loop` skill
self-paces via the host scheduler (`/loop`, OS cron, or the runtime's task scheduler), and
`orient_clamp.py` is invoked directly. See `adapters/<runtime>/` for the per-runtime entry.

## Safety

- Fail-open everywhere: errors → stop allowed / command unchanged.
- `orient_rewrite.py` never rewrites writes, excluded, or compound commands (`&& | ; > $()`).
- The loop never exits on a self-reported "done" — only on an evidence-backed `<promise>`,
  the `max_iterations` cap, the budget kill-switch, or an explicit `.orchestrator/STOP`.
- Treat `.orchestrator/orient.toml` as untrusted perception-shaping config: review + hash-pin
  before trusting it (see `simplicio-orient`).
