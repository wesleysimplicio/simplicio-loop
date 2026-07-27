# Coverage Atlas ↔ Virtual Custodians v1

This integration keeps the authority boundaries from issue #784 explicit:

- **Mapper observes.** It emits a content-addressed incremental
  `simplicio.coverage-delta/v1`; it never starts work.
- **Fast protects.** It advertises inert `CustodianAddressV1` values and returns
  a bounded receipt/verdict only for an authorized envelope. `FIXED` is a
  report, not completion.
- **Loop decides.** `simplicio_loop.coverage_custodians` owns policy, budget,
  dispatch authorization, ledger transitions and terminality.

## Causal flow

1. Validate and normalize the Mapper delta. Gap IDs bind atlas digest, kind and
   subject; the delta is stable regardless of input ordering.
2. The Loop maps Fast-owned gap kinds to the newest compatible virtual address.
   Addresses remain data and do not imply a process.
3. Loop policy chooses `DISPATCH`, `DEFER`, or `NOT_APPLICABLE`. A budget
   is consumed only for dispatch.
4. Only a `DISPATCH` decision can create a `FastWorkEnvelopeV1`. Its
   idempotency key binds gap, run, fence and plan revision.
5. Fast returns a receipt bound to that envelope. Invalid digests, stale fences,
   mismatched gaps, absent evidence and unknown verdicts fail closed.
6. `FIXED` moves the ledger only to `REPORTED_FIXED`.
7. The original gap becomes `VERIFIED` only when a later Mapper delta no
   longer contains it and a different agent supplies an independent PASS with
   evidence.
8. `terminal()` is Loop-owned and returns true only when every ledger entry is
   `VERIFIED`.

## Contract summary

| Schema | Producer | Consumer | Authority |
|---|---|---|---|
| `simplicio.coverage-delta/v1` | Mapper | Loop | observation |
| `simplicio.custodian-address/v1` | Fast | Loop | capability advertisement |
| `simplicio.fast-work-envelope/v1` | Loop | Fast | dispatch authorization |
| `simplicio.custodian-receipt/v1` | Fast | Loop | execution evidence |
| `simplicio.fast-verdict/v1` | Fast | Loop | scoped repair verdict |
| `simplicio.work-gap-ledger/v1` | Loop | Completion Auditor | authoritative state |

## Integration example

```python
from simplicio_loop import coverage_custodians as bridge

decisions = bridge.decide(mapper_delta, virtual_addresses, {
    "dispatch_budget": 20,
    "deferred_gap_ids": [],
    "not_applicable_gap_ids": [],
})

# The host executes Hookwall before handing an envelope to Fast.
envelope = bridge.build_envelope(gap, decisions[0], {
    "run_id": run_id,
    "fence": fence,
    "plan_revision": plan_revision,
}, {"cpu_ms": 500, "max_attempts": 1})

ledger = bridge.reduce_ledger(
    previous_ledger,
    mapper_delta,
    decisions,
    envelopes=[envelope],
    receipts=[fast_receipt],
    verification_delta=mapper_rescan_delta,
    verifier=independent_verifier_receipt,
)
```

The reducer performs no I/O and does not materialize workers. Hookwall, process
creation, persistence, timeout, cancellation and backpressure remain host
responsibilities; their receipts should be attached as evidence before this
slice is expanded into the installed-artifact E2E lane.

## Failure semantics

- Missing custodian or exhausted budget: `DEFER`.
- Authorized dispatch without envelope: `BLOCKED`.
- Invalid or unbound Fast receipt: `BLOCKED`.
- Fast reports no change: gap remains `OPEN`.
- Fast reports fixed: `REPORTED_FIXED`, never delivered.
- Mapper still sees the gap: gap returns to `OPEN`.
- Mapper no longer sees it without independent verification: `BLOCKED`.
- Previously verified gap reappears: `BLOCKED` as regression.

The unit suite covers deterministic IDs/order, clean control, budget,
non-Fast ownership, address generation selection, idempotency, receipt binding,
tampering, self-verification, rescan disagreement and replay stability.
