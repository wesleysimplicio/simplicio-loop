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

`HookwallEffectLedger` is the production persistence boundary. It uses a
SQLite WAL with `synchronous=FULL` and an atomic reservation before execution.
Only one caller may execute an idempotency key; verified retries reuse compact
evidence, while reserved, effect-confirmed, or uncertain retries fail closed
for reconciliation. Its append-only hash chain detects offline tampering.

The sealed envelope also contains the exact write set and allowlisted command.
Traversal, globs, absolute paths, symlink escapes, and non-Dev-CLI mutation
commands are rejected before reservation.

`HookwallRollout` persists shadow, canary, enforced, and rollback transitions
using fsync plus atomic replacement. All four modes retain
`mutation_requires_hookwall=true`; rollout changes observation and routing,
never whether a mutable effect must pass the boundary.

The cross-repository inventory is
`contracts/hookwall/v1/entrypoints.json`. Loop's entrypoint is verified. The
Runtime Stage ABI and Dev CLI apply/rollback entrypoints remain explicit open
dependencies in Runtime #3629 and Dev CLI #353. Therefore Loop issue #783 must
remain open until those repositories publish compatible, tested evidence.
