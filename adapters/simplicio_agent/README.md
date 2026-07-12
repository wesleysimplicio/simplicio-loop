# Simplicio Agent adapter

Simplicio Agent (formerly Hermes) is a native simplicio runtime: it has a real agent/sub-agent
fabric and binds the extension points directly (no MCP shim needed). This is the deepest
integration — most steps run deterministically at near-zero token cost.

## Install

```bash
bash scripts/install.sh simplicio_agent
```

The installer places the 6 skills where Simplicio Agent's skill-recall finds them and confirms
the native bindings are active.

## Loop drive — native loop

Simplicio Agent drives the loop natively (its scheduler IS the watcher). `simplicio-loop` binds
to the native durable scheduler; the evidence-gated completion-promise, cap, and STOP guards are
enforced by the runtime, not a shell hook.

## Native bind — extension points (the whole point, REQUIRED)

`simplicio-runtime` native bind is REQUIRED on Simplicio Agent, not optional — there is no
unbound fallback worth running here; if the bind isn't live, STOP and fix it rather than letting
the orchestrator quietly fall back to a plain-LLM loop on a host built for native binding. Verify
with `simplicio doctor --json` before relying on it.

Simplicio Agent binds, among others: `orient`, `recall`, `deterministic_edit`, `claim`,
`worktree`, `diagnostics`, `validate`, `pr`, `watcher`, `savings_ledger`, `model_route`. When
bound, the orchestrator delegates to them and the satellite skills become near-free:

| Satellite | Native binding |
|---|---|
| simplicio-orient | `orient` · `shell_exec` · `compress` (native clamp + tee) |
| simplicio-loop | `watcher` · `durable_workflow` (native loop) |
| simplicio-review | native parallel sub-agent fan-out |
| simplicio-compress | native `transform_guard` + `savings_ledger` |
| simplicio-learn | native `trajectory` · `learn` · `recall` |

## Token economy

Native: the runtime measures REAL token spend via `savings_ledger`; the savings line is exact,
not estimated.

## Use

```
simplicio-agent run "/simplicio-tasks finish all the open issues"
```

## Migrating from Hermes

`simplicio-agent` is the same binary/CLI previously distributed as `hermes` — the config path
moved from `~/.hermes/config.yaml` to `~/.simplicio-agent/config.yaml` and logs from
`~/.hermes/logs/` to `~/.simplicio-agent/logs/`. The [`hermes` adapter](../hermes/README.md) is
kept as a legacy shim for one release cycle; scripts in this repo detect either binary and warn
when they fall back to the legacy one.
