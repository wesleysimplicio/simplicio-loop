# Hookwall mutation gate (issue #783)

The Loop now has a transport-neutral, fail-closed boundary for mutable work.
A mutable dispatch is eligible for execution only when its
`DispatchEnvelopeV1` is complete and a lineage-bound `hookwall_pre` decision
returns `proceed`. After execution, the Loop accepts the result only when a
content-addressed `MutationReceiptV1` and a receipt-bound `hookwall_post`
decision agree.

## Authority flow

1. Loop seals the envelope: run/plan identity, source and policy hashes,
   workspace, fence, resolved effect set, and idempotency key.
2. Runtime Hookwall returns a pre decision bound to the envelope hash.
3. Dev CLI applies only the resolved effect set and returns a mutation receipt.
4. Runtime Hookwall returns a post decision bound to that receipt hash.
5. Loop builds compact Hookwall evidence and independently gates completion.

Unknown effects, missing hooks, source/policy/fence drift, altered receipts,
blocked decisions, or duplicate idempotency keys raise `HookwallBlocked`.
There is no mutating fallback. Runtime and Dev CLI never declare work complete.

## Integration contract

Call `validate_pre_decision` immediately before any side effect, persist the
idempotency set durably in the caller, then call `verify_post_receipt`. Feed
the resulting evidence into `gate_completion` alongside the existing
Completion Auditor checks. The in-memory set accepted by these pure functions
is a testable adapter; production owns durable storage and atomic commit.
