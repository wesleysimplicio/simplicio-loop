# Shared run budget contract

`simplicio_loop.budget` is the local control-plane primitive for issue #226. A
`BudgetLedger` stores one immutable `RunBudget` envelope in SQLite and every lane
must reserve estimated capacity before dispatch. `BEGIN IMMEDIATE` serializes
admission across processes, so parallel workers cannot each spend the full budget.

Reservations are keyed by `reservation_id`. Repeating the same reservation with the
same estimate is idempotent; changing an estimate fails closed. A settlement moves a
reservation into spent usage exactly once. Cancellation returns reserved capacity,
while a late receipt is rejected if it would overspend the envelope. The snapshot
reports spent and reserved tokens/calls/cost/latency for operator inspection.

The same module defines two continuation primitives:

- `ContextPackRef` hashes the immutable goal, policy, and acceptance prefix. A
  repository/config fingerprint is kept separately, so a changed relevant tree can
  force a remap without invalidating the stable goal hash.
- `continuation_delta()` requires positive event sequence numbers and returns only
  events after the acknowledged cursor. A full-history resend is represented only by
  the explicit `force_full=True` flag, making accidental history resends testable.

The schemas are versioned (`simplicio.run-budget/v1`,
`simplicio.budget-reservation/v1`, `simplicio.usage-settlement/v1`,
`simplicio.context-pack-ref/v1`, and `simplicio.continuation-delta/v1`) so runtime
adapters can reject incompatible receipts instead of silently over-running a run.
