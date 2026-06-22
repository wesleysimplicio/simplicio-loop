# Hermes adapter

Hermes is a native simplicio runtime: it has a real agent/sub-agent fabric and binds the
extension points directly (no MCP shim needed). This is the deepest integration — most steps run
deterministically at near-zero token cost.

## Install

```bash
bash scripts/install.sh hermes
```

The installer places the 6 skills where Hermes' skill-recall finds them and confirms the native
bindings are active.

## Loop drive — native loop

Hermes drives the loop natively (its scheduler IS the watcher). `simplicio-loop` binds to the
native durable scheduler; the evidence-gated completion-promise and the cap/budget guards are
enforced by the runtime, not a shell hook.

## Native bind — extension points (the whole point)

Hermes binds, among others: `orient`, `recall`, `deterministic_edit`, `claim`, `worktree`,
`diagnostics`, `validate`, `pr`, `watcher`, `savings_ledger`, `model_route`. When bound, the
orchestrator delegates to them and the satellite skills become near-free:

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
hermes run "/simplicio-tasks finish all the open issues"
```
