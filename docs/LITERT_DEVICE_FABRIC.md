# LiteRT device fabric

`simplicio_loop.device_fabric` connects Loop stages to Runtime-owned physical
CPU, GPU, and NPU capacity. The Loop never imports or starts LiteRT,
LiteRT-LM, a model, or a provider. LiteRT detection reads package metadata
only; the Runtime capacity snapshot remains authoritative for actual devices.

## Boundary

- Loop owns logical queueing, session fairness, priorities, convergence and
  bounded `queue_capacity`/`max_in_flight`.
- Runtime owns physical device slots, memory budgets, opaque leases, fences,
  deadlines, execution, cancellation, release and reconciliation.
- A logical Loop slot never implies a process, model instance or device slot.
- Multiple Loop hosts may share one Runtime authority; acquisition remains
  exclusive and fenced.

Requirements are abstract: `completion`, `embedding`, or `vision`, plus
latency/memory classes. Backend flags never cross the Loop API.

## Admission and fallback

Runtime snapshots are revisioned and TTL-bound. Stale snapshots fail closed.
The queue is hard bounded and uses round-robin sessions. Runtime capacity and
memory stay bounded independently per device. Pressure snapshots reduce the
logical admission wave before an OOM.

Fallback requires the effective device to appear in
`allowed_fallback_devices`. The receipt records requested capability/devices,
effective backend/device, capacity revision, opaque lease ID/fence, fallback
decision, queue time and execution time separately.

## Cancellation and uncertainty

Queued cancellation removes the request before acquisition. Running
cancellation is propagated to Runtime, cancels the owned coroutine and
releases the lease. Timeout follows the same release path.

Only classified transient failures retry, preserving the same idempotency key.
An unknown effect is reconciled with Runtime and is never executed again
without a terminal Runtime receipt. Duplicate active/terminal causal
identities fail closed.

When LiteRT is unavailable, only stages requiring its device fabric are
blocked; independent lanes continue. Every degraded/fallback state is explicit
and `model_provider_started` remains false.

## Evidence

```bash
python3 scripts/benchmark_device_fabric_794.py \
  --repeats 5 \
  --output tests/fixtures/device_fabric_benchmark_794.json

python3 scripts/device_fabric_e2e_794.py \
  --output tests/fixtures/device_fabric_e2e_794.json \
  --evidence-dir /tmp/simplicio-device-evidence
```

The benchmark measures 1, 6 and 64 logical tasks against two CPU slots, one
GPU slot and one NPU slot. The E2E uses two Loop hosts sharing one Runtime
authority, exercises explicit NPU→CPU fallback, persists hash-checked receipts
and proves lease release.

## Rollback

Revert the device-fabric commit. Runtime leases are opaque and expire/release
independently; saved receipts are evidence only and cannot acquire capacity.
