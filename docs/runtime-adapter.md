# Loop→Runtime adapter contract

`simplicio_loop.runtime_adapter.LoopRuntimeAdapter` is the canonical, transport-neutral
bridge for a runtime binding. CLI, MCP, Desktop, and HTTP clients implement the same two
methods (`negotiate` and `apply`); they do not invent separate Run/WorkItem identities.

## Modes and failure behavior

| Mode | How it starts | Completion claim | Recovery |
| --- | --- | --- | --- |
| `runtime` | `transport=` plus successful `negotiate()` | Delivered only after the runtime accepts the operation | immediate apply |
| `degraded` | runtime apply/negotiation fails | never claims Done; operations are `BUFFERED` | `reconcile()` replays the durable outbox |
| `standalone` | explicit `standalone=True` | local receipt only; never says runtime delivered | caller may import the outbox |

The adapter rejects a missing/unsupported `simplicio.runtime/v1` contract before mutation. Every
operation carries `run_id`, `work_item_id`, `actor`, a unique `operation_id`, and the adapter
contract version. Event payloads are validated by the Loop phase-event state machine before they
cross the boundary. The outbox is JSONL, so duplicate delivery is safe when the Runtime treats
`operation_id` as its idempotency key.

```python
bridge = LoopRuntimeAdapter(
    run_id="run-...", work_item_id="wi-...", actor="claude@host-b",
    transport=runtime_client, outbox_path=".orchestrator/runtime-outbox.jsonl",
)
bridge.negotiate()              # fails closed on contract mismatch
bridge.register_run(manifest)
bridge.emit_event(phase_event)
bridge.record_evidence(receipt)
bridge.complete(completion_receipt)  # only ready=True, verdict=COMPLETE
bridge.reconcile()               # after reconnect
```

The standalone path is intentionally explicit. A missing runtime bind is not silently treated as
runtime success, and an outage cannot create a false completion.
