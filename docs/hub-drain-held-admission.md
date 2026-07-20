# Held GitHub drain admission (#629)

`simplicio-loop hub-drain-admit --checkpoint <final-627.json> --endpoint <hub>` validates
and projects a final #627 checkpoint, then asks an already-running Hub to persist one root
job in state `admitted_held`. The command returns exit code `3` and status
`ADMITTED_NOT_DISPATCHED`; admission is deliberately not execution success.

The checkpoint projector is fail-closed. It verifies integrity and run identity, the final
`PLANNED_NOT_EXECUTED` outcome, `execution_authorized=false`, the source observation, a ready
canonical map, non-empty items, and dependency waves reconstructed by the #627 planner. The
durable job is `simplicio.github-drain-job/v1`, has `dispatchable=false`,
`activation_required=true`, and omits checkpoint/workspace absolute paths. Its idempotency key
is derived from the checkpoint run digest; callers do not provide an arbitrary key. The job
exposes canonical run, checkpoint, source, plan, and opaque workspace digests plus issue count,
without persisting the workspace or checkpoint path.

`HubRetryQueue.admit_held()` writes the `hub_jobs` row and
`simplicio.hub-admission-receipt/v1` row under one SQLite `BEGIN IMMEDIATE` transaction.
Replays require the same canonical job, input digest, client, workspace, weight and cost.
Held jobs are excluded from claims and scheduler rehydration, and their payload cannot be
updated. The receipt's capacity snapshot is only a sanitized observation; it reserves no
capacity and must be refreshed before any future activation.

## Deliberate limitations

- There is no activation, release, scheduling, governor reservation, claim, worker, runner,
  worktree or GitHub mutation in this slice.
- The CLI never starts a Hub. A missing/unreachable endpoint is a non-zero failure.
- Admission stores one root job, not one executable job per issue or wave.
- A future activation boundary must revalidate source/map freshness, take a fresh capacity
  snapshot, authorize execution explicitly, and define its own atomic state transition.
