# Automatic fan-out contract

`simplicio-loop batch` uses isolated worktrees automatically when the selected tasks are
independent and the run has authorized plan targets. This is the default behavior:

```powershell
simplicio-loop batch --repo . <run-id>
```

The scheduler freezes the task contract, builds impact keys from each plan step, rejects
overlapping targets for concurrent execution, registers a durable queue under
`.orchestrator/runs/<run-id>/worktree-queue.json`, and allocates one owned worktree per
task before starting the operator pool. The JSON result exposes:

```json
{
  "fan_out": {"enabled": true, "default": true, "contexts": 3, "reason": ""},
  "max_workers": 3,
  "workers": [{"worktree_context": {"mode": "worktree"}}]
}
```

The fallback is deliberately conservative. Missing targets, overlapping impact keys,
non-Git checkouts, an unavailable adapter, queue preflight failure, or an existing shared
context all run serially and expose `serial_fallback_reason`; no parallel claim is emitted.
To force the serial lane for a legacy repository, use either:

```powershell
simplicio-loop batch --serial --repo . <run-id>
$env:SIMPLICIO_LOOP_AUTO_FAN_OUT = "0"
```

Each worker's context and branch are persisted by `WorktreeQueue`; the source checkout is
never checked out or mutated by the coordinator while workers are active. Fan-out does not
merge candidates automatically: composed verification and the delivery gate remain required.
