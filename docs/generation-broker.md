# Generation broker

`GenerationBroker` is the local facade that binds a map-service canonical cache
entry to a checkpoint-lifecycle candidate overlay. Candidates sharing the same
repository identity, tree hash, and file set reuse the canonical cache key while
receiving distinct writable overlay paths.

Each binding returns a deterministic receipt hash over the candidate, canonical
cache key, canonical generation receipt, and resolved overlay path. A binding is
rejected before creating an overlay when its generation differs from the
checkpoint lifecycle generation.

Garbage collection delegates to `CheckpointLifecycle.gc`. Active leases pin
candidate overlays; cancelled candidates become reclaimable only after their
lease and configured retention window expire.

Run the focused benchmark with:

```powershell
python -m bench.benchmark_generation_broker_888
```
