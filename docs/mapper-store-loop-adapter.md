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
```

Both return `simplicio.loop-store-adapter/v1` and include the selected route,
generation, run identity, writer authority, capability report and
`effects_attempted=false`. No probe creates a directory, database, lock or
receipt. A frozen route can be serialized as
`simplicio.loop-store-route-receipt/v1` for the caller's durable run evidence.

This slice does not migrate queue, journal or Hookwall tables. Those remain
separate follow-up slices (#1026 and #1028), and the final cutover is #1027.
