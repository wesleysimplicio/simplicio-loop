# TelemetryEvent/v1

The telemetry writer emits crash-durable, hash-chained JSONL. Every metric has
`value`, `unit`, `origin` and `reason`; an unavailable value must be `null`
with an explicit reason.

Stage spans record queue wait, execution, retry, cold/warm cache label, process
CPU, peak RSS, process I/O and optional provider token/cache/context usage.
Collectors never estimate missing measurements.

Causal IDs are hashed. Labels use a fixed allowlist and cardinality bound.
Secret, authorization, prompt, content, email and PII-shaped fields are
redacted recursively. Each event is flushed and fsynced before `emit` returns,
including the exception path.

`reconcile()` verifies hashes/causality and derives totals directly from raw
events. `scripts/benchmark_telemetry.py` measures real per-event overhead,
including flush/fsync:

```bash
python scripts/benchmark_telemetry.py --iterations 100 --samples 7
```

No LLM or provider is started by collection.
