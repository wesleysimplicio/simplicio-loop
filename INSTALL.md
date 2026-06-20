# Install — simplicio-tasks

simplicio-tasks is a **skill**, not a binary. There is nothing to compile and no
dependency to install. You copy one folder into your agent runtime.

## 1. Get the skill

```bash
git clone https://github.com/wesleysimplicio/simplicio-tasks
```

## 2. Drop it into your runtime

**Claude Code** — project-local or global:

```bash
# project-local (this repo's agents only)
cp -r simplicio-tasks/.claude/skills/simplicio-tasks  <your-repo>/.claude/skills/

# global (all projects) — user skills dir
cp -r simplicio-tasks/.claude/skills/simplicio-tasks  ~/.claude/skills/
```

**Codex / Gemini / Copilot / local agents** — load the same file:

```
simplicio-tasks/.claude/skills/simplicio-tasks/SKILL.md
```

See [`AGENTS.md`](AGENTS.md), [`CLAUDE.md`](CLAUDE.md), [`GEMINI.md`](GEMINI.md) for
per-runtime entry points.

## 3. Run it

```
/simplicio-tasks finish all the open issues
```

## 4. (Optional) Before an unattended 24/7 run

Create a cost kill-switch so the watcher is allowed to run while you sleep:

Create `.orchestrator/loop-budget.json` with your editor (cross-platform — the bash
heredoc below is just one way; on Windows/PowerShell use your editor or `Set-Content`):

```bash
mkdir -p .orchestrator
cat > .orchestrator/loop-budget.json <<'JSON'
{
  "daily_usd_ceiling": 5.00,
  "per_run_token_ceiling": 0,
  "spent_usd_today": 0,
  "reset_at": "<next local midnight, UTC ISO-8601, e.g. 2026-06-20T00:00:00Z>",
  "state": "running"
}
JSON
```

Set `reset_at` to the next midnight (not a past date, or the budget resets immediately).
With `daily_usd_ceiling = 0` (or no file) the watcher **refuses** to run unattended —
that is the intentional fail-safe.

## Requirements

- A strong LLM agent runtime (Claude Code, Codex, Gemini, Copilot, or a local agent).
- `git` and, for GitHub sources, the `gh` CLI authenticated.
- That's it. Every extension point has an LLM fallback, so no native runtime is
  required — though if one is present it makes the skill faster and cheaper.
