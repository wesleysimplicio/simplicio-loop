# Codex adapter

Codex reads `AGENTS.md` as its standing instructions. Point it at the skill; drive the loop
self-paced (Codex has no general stop-hook); bind natively via MCP or the Python adapter.

## Install

```bash
bash scripts/install.sh codex            # writes/links AGENTS.md → SKILL.md, copies skills
```

The installer ensures `AGENTS.md` at the repo root references
`.claude/skills/simplicio-tasks/SKILL.md` (this repo's `AGENTS.md` already does). Codex loads
that on every run.

## Loop drive — self-paced

Codex has no stop-hook, so `simplicio-loop` self-paces: each run does one iteration, checks the
evidence-gated promise, and reschedules itself via the host scheduler until the promise is true,
the cap is hit, spindle handoff is latched, or STOP is signaled. Drive ticks with `codex exec`
on a cron / CI schedule:

```bash
*/2 * * * *  cd /repo && codex exec "/simplicio-tasks continue the open queue"
```

## Token economy

`orient_clamp.py` works as-is. Add it to your `AGENTS.md` command conventions so Codex routes
heavy commands through it:

```
python3 hooks/orient_clamp.py -- <build/test/diff command>
```

## Native bind (REQUIRED)

`simplicio-runtime` native bind is REQUIRED on Codex, not optional — `scripts/install.sh codex`
forces this automatically; by hand:

```bash
pip install -U simplicio-installer && simplicio install --global   # registers Codex's MCP client
# or use the Python adapter at simplicio-runtime/agent/codex_responses_adapter.py
```

Verify with `simplicio doctor --json` before driving the loop; if unreachable, STOP and report
the gap rather than self-pacing on the unbound LLM-only fallback.

## Use

```
codex exec "/simplicio-tasks finish all the open issues"
```
