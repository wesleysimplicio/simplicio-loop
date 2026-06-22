# Aider adapter

Aider is a pair-programming CLI with no skill system and no hooks. The adapter inlines the
protocol as Aider's conventions file and self-paces the loop from the shell. Everything degrades
to the LLM fallback — same gates, larger context.

## Install

```bash
bash scripts/install.sh aider
```

The installer writes `CONVENTIONS.md` (the orchestrator protocol, condensed via
`simplicio-compress` so it costs less every turn) and configures `.aider.conf.yml` to always
read it:

```yaml
read: [CONVENTIONS.md]
```

## Loop drive — self-paced from the shell

No hooks. Drive the loop with Aider's non-interactive mode on a tick:

```bash
*/2 * * * *  cd /repo && aider --message "/simplicio-tasks continue the open queue" --yes-always
```

`simplicio-loop` runs in self-paced mode: one iteration per invocation, exit on the
evidence-gated promise, the cap, or the budget kill-switch. Keep `--yes-always` OFF for
irreversible-op safety unless a human gate is otherwise wired.

## Token economy

This matters most here (no native bind). Route every heavy command through the wrapper:

```bash
python3 hooks/orient_clamp.py -- pytest -q
```

and keep `CONVENTIONS.md` compressed (input-side savings amortized across every turn).

## Native bind

None. The LLM performs each extension point with git/gh/file tools — the documented fallback.

## Use

```
aider --message "/simplicio-tasks finish all the open issues"
```
