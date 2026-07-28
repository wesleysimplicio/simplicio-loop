# ADR 0005: Prism component authority

Status: accepted

## Context

Prism execution crosses Mapper, Fast, Dev CLI, Loop, and the optional Runtime.
Without one authority boundary, an accelerator could accidentally schedule,
mutate, or promote completion.

## Decision

- Mapper owns observed repository facts, dependency edges, write sets, and
  conflict predictions. It does not execute effects or close work.
- Fast owns content-addressed read snapshots, mmap arenas, overlays, cache, and
  hot-path queries. It does not schedule or promote completion.
- Dev CLI owns bounded external effects and their intent/result receipts. It
  does not select the next task or interpret goal completion.
- Loop owns hierarchy, scheduler, accountable agent assignments, leases,
  fences, reducer, recovery coordination, and completion inputs.
- Runtime may accelerate the frozen contracts when its binary is healthy and
  compatible. Rust is preferred, never required, and never widens authority.
- Only an independent completion oracle may promote a verified terminal result.

Each task attempt has exactly one accountable owner. Helpers may contribute
evidence but cannot issue its terminal receipt. Review and completion agents
must be independent of the implementation owner.

Internal append/replay uses checksummed HBP frames and a hash chain. JSON is an
external export and conformance format, not the source of truth. Unknown schema
versions and fields fail closed. Legacy envelopes can be inspected but are
always non-authoritative.

## Consequences

Python alone can execute the full hierarchy. Runtime failure produces a
reason-coded Python fallback. Device loss increments the lease fence before
reassignment. A task receipt never completes its slot or prism; deterministic
reducers reconverge child evidence and the completion oracle decides terminal
promotion.
