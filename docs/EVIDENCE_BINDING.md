# Derived-evidence binding and invalidation

`simplicio.evidence-binding/v1` binds every derived quality, watcher, delivery, and
completion PASS to the run, task, attempt, Git HEAD/tree/diff (including untracked
content), policy, configuration, toolchain, and task contract that were measured.
Consumers call `validate_receipt_binding` against a newly captured binding. Missing
bindings are legacy evidence and fail with `evidence_binding_missing`; migration is
an explicit re-execution, never an assumption of freshness.

Every observable mutation calls `invalidate_derived_evidence`. The function atomically
appends an idempotent tombstone to `evidence-invalidation.jsonl` and retains the old
receipts for audit. Rebase, squash, merge, conflict resolution, retry, human decisions,
source refresh, and policy-only changes use reason codes plus the changed binding
fields. A crash before the tombstone remains fail-closed because consumers recompute
HEAD/tree/diff and the other source hashes. Selective lane freshness is allowed only
when a dependency proof is supplied and persisted in the tombstone; otherwise all four
receipt classes are invalidated.

Benchmark: `python3 scripts/evidence_binding_bench.py --iterations 100000` prints raw
elapsed time, latency per validation, and throughput as JSON.
