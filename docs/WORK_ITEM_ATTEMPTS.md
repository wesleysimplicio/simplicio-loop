# Bounded WorkItem attempts

`simplicio_loop.work_item_claims.AttemptCoordinator` is the local bridge between the
state-machine runner and the Runtime queue lease:

1. `claim()` atomically obtains one fenced lease and builds an allow-listed context pack.
2. `record_event()` and `accept_receipt()` first call `assert_active()`, so a stale worker
   cannot append accepted tool/evidence output after expiry or reassignment.
3. `retry()` releases the current lease and claims a new fencing token/attempt.
4. `complete()` persists the queue completion only after the current lease is checked.

SQLite and the HTTP queue facade implement the same read-only `assert-active` operation. A
queue outage raises `QueueUnavailable`; callers must hand off rather than mutate. This is a
measured local contract, not proof that an external board or multi-device service is deployed.

```python
from simplicio_loop.remote_queue import SQLiteRemoteQueue
from simplicio_loop.work_item_claims import AttemptCoordinator

attempt = AttemptCoordinator(queue, run_id="run-1").claim(
    work_item_id="WI-1", identity=identity, goal="...", acs=["AC-1"],
    allowed_paths=["src/feature.py"],
)
coordinator.accept_receipt(attempt, {"status": "passed"})
coordinator.complete(attempt, receipt_ref="receipts/WI-1.json")
```

Focused proof lives in `tests/test_work_item_claims.py` and the HTTP assertion is covered by
`tests/test_remote_queue.py`.

## Planning receipt + mutation authority (#284)

`execute_operator()` already refuses to run without fresh mapper/plan/operator preflight
receipts, a passing `plan_contract.validate_plan()`, an unchanged repo state since planning, and
an operator target confined to the plan's `candidate_targets`. Issue #284 asked for those checks
to be tied together into one explicit, hash-bound artifact plus a derived **mutation authority**
token, so a caller can never invoke the mutation boundary while silently skipping part of the
intake — `simplicio_loop/planning_gate.py` (CLI: `scripts/planning_gate.py`) is that artifact:

```python
from simplicio_loop.plan_contract import validate_plan
from simplicio_loop.planning_gate import build_planning_receipt

plan_validation = validate_plan(plan, tasks, repo, contract_hash=contract["collection_hash"])
receipt = build_planning_receipt(
    run_id=run_id, attempt=attempt, contract=contract, plan=plan,
    plan_validation=plan_validation, lease_id=lease_id, fencing_token=fencing_token,
)
# receipt["mutation_authority"] is "" unless plan_validation["valid"] is True — a bad
# plan can never mint a mutation authority.
```

`evaluate_mutation_authority(run_dir, run_id=..., attempt=..., task_contract_hash=...,
plan_hash=..., lease_id=..., fencing_token=...)` re-derives the expected token from the
identity tuple **being executed right now** and fails closed
(`planning_receipt_missing`/`planning_receipt_schema_invalid`/`planning_not_ready`/
`mutation_authority_invalid`) on any mismatch — a stale plan hash (repo/plan drifted after
planning), a rotated lease/fencing token (lease lost/reassigned), or a missing/corrupt receipt
all invalidate the authority rather than being silently accepted.

`execute_operator()` wires this in as an **opt-in** gate: set
`SIMPLICIO_REQUIRE_MUTATION_AUTHORITY=1` to require a valid `planning-receipt.json` before any
mutation. It defaults unset (zero behavior change for every existing caller/fixture) because
flipping it to mandatory-by-default needs every run in the test suite to build a planning
receipt first — tracked as follow-up, not claimed done here. Also not yet implemented: GitHub
source-revision capture (depends on the sibling issue #285's adapter), the full
`simplicio.task-intake/v1` envelope (scope in/out, dependencies, risks, rollback, impact map),
and plan v2's DAG/parallelizable-step metadata.

Tests: `tests/test_planning_gate_unit.py` (token determinism, receipt building, fail-closed
re-verification) and `tests/test_planning_gate_execute_operator_integration.py` (the opt-in flag
is genuinely zero-behavior-change when unset, blocks without a receipt, allows with a valid one,
and blocks again once the plan drifts after the receipt was minted).
