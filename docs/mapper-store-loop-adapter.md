# Loop MapperStore adapter (#1025)

Loop retains ownership of the DAG, readiness, retries, delivery and
completion. `simplicio_loop.store_adapter` owns only storage route selection
and capability evidence.

The default is `legacy`. `shadow` and `mapper` are explicit and fail closed if
the installed `simplicio-mapper` API is absent, below `0.26.1`, or missing a
required capability. Selection is immutable after the first write, claim or
effect intent; there is no silent fallback.

The read-only surfaces are:

```text
simplicio-loop doctor --storage --route legacy --json
simplicio-loop inspect --storage --route mapper --json
simplicio-loop doctor --storage --repo . --json
```

The runner freezes a `simplicio.loop-store-route-receipt/v1` in each run before
the Mapper scan.  A later route or capability change blocks the run before a
claim or effect intent; there is no automatic fallback.

Both commands return `simplicio.loop-store-adapter/v1` and include the selected route,
generation, run identity, writer authority, capability report and
`effects_attempted=false`. No probe creates a directory, database, lock or
receipt. A frozen route can be serialized as
`simplicio.loop-store-route-receipt/v1` for the caller's durable run evidence.

When `--repo` is supplied, the JSON also contains a read-only
`simplicio.storage-cutover-doctor/v1` report. It classifies known legacy stores
as `LEGACY_PRESENT`, `SPLIT_BRAIN`, `CORRUPT`, or `MIGRATING`, and marks a
canonical-only installation `CLEAN`. It never creates a directory, opens a
database for writing, copies rows, or runs migration DDL.

This slice does not migrate queue, journal or Hookwall tables. Those remain
separate follow-up slices (#1026 and #1028), and the final cutover is #1027.
