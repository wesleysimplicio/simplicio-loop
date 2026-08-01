# Generation broker

`GenerationBroker` is the local facade that binds a map-service canonical cache
entry to a checkpoint-lifecycle candidate overlay. Candidates sharing the same
repository identity, tree hash, and file set reuse the canonical cache key while
receiving distinct writable overlay paths.

Each binding returns a deterministic receipt hash over the candidate, canonical
cache key, repository/tree/config identity, source/context/plan receipts,
worktree, attempt, lease, and resolved overlay path. A binding is
rejected before creating an overlay when its generation differs from the
checkpoint lifecycle generation.

The persisted `generation-binding.json` uses
`simplicio.loop.generation-binding/v1`. `inspect`, `pin`, `release`, `reconcile`,
`doctor`, `status`, and `promote` provide the durable local operator surface.
Broker events and cache/build-wait counters are returned by `status`.

Garbage collection delegates to `CheckpointLifecycle.gc`. Active leases pin
candidate overlays; cancelled candidates become reclaimable only after their
lease and configured retention window expire.

Run the focused benchmark with:

```powershell
python -m bench.benchmark_generation_broker_888
```

Persisted attempts expose JSON operations through `simplicio-loop generation-broker`:
`inspect`, `pin`, `release`, `reconcile`, `status`, and `doctor`.
