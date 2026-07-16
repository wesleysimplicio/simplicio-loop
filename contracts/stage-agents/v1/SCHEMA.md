# `simplicio.stage-agents/v1` — Portable Stage Agents contract

Issue [#423](https://github.com/wesleysimplicio/simplicio-loop/issues/423), epic
[#422](https://github.com/wesleysimplicio/simplicio-loop/issues/422) "Portable Stage Agents".

This is the **single canonical contract** that turns a role described in a skill into a concrete
agent instance bound to one stage of a run's stage graph. It does not create business-specific
agents — later epic-#422 issues build on top of the schemas, invariants, lifecycle, and manifest
defined here.

It **extends** `simplicio_loop/agent_contract.py` (`simplicio.agent-context/v1` /
`simplicio.agent-receipt/v1`) by composition — `simplicio_loop/stage_agents.py` wraps
`build_context_pack()`/`bind_receipt()` and adds the role/stage/run/task/attempt/fence/
plan_revision fields, isolation level, and stage-graph reducer this issue requires — rather than
replacing it with a parallel structure (issue invariant: "must be extended with compatibility, not
duplicated").

## The seven schemas

| File | Schema id | Purpose |
|---|---|---|
| `stage-definition.schema.json` | `simplicio.stage-definition/v1` | one stage's owner role, deps, isolation, gates, timeout/retry, next stages |
| `role-definition.schema.json` | `simplicio.role-definition/v1` | a role's mission/boundary, allowed tools/capabilities, pass/fail criteria, hash-pinned prompt |
| `agent-instance.schema.json` | `simplicio.agent-instance/v1` | one materialized agent bound to run/task/attempt/fence/plan_revision + lifecycle timestamps |
| `stage-input.schema.json` | `simplicio.stage-input/v1` | the payload handed to an agent instance for its stage |
| `stage-output.schema.json` | `simplicio.stage-output/v1` | the payload an agent instance produced for its stage |
| `stage-receipt.schema.json` | `simplicio.stage-receipt/v1` | typed pass/fail/blocked verdict with hashes, evidence, ACs covered, next-stage recommendation |
| `run-stage-graph.schema.json` | `simplicio.run-stage-graph/v1` | the DAG of stages for one run, derived mechanically from `stages.json` |

`stages.json` is the frozen, versioned manifest instantiating the canonical epic-#422 agents
(`coordinator → implementer → {reviewer, tester} → integrator`) as `RoleDefinition` +
`StageDefinition` objects. Every field the issue requires (owner/inputs/outputs/capabilities/
isolation/gates/timeout/retry/fallback/receipt) is populated per stage — see
`scripts/stage_agents.py validate` to check it mechanically instead of by inspection.

## Validator

`simplicio_loop/stage_agents.py` ships a small, dependency-free JSON-Schema subset validator
(`type`/`const`/`enum`/`required`/`properties`/`additionalProperties: false`/`items`/`pattern`/
`minimum`/`maximum`) — enough for these seven schemas, keeping the repo's stdlib-only posture. It
fails closed on any unknown field when `additionalProperties` is `false` (invariant 7).

## Invariants enforced (see the issue for the full list)

1. `bind_stage_receipt` refuses a receipt missing `role_id`/`stage_id`.
2. `check_receipt_freshness` rejects a receipt from another run/task/attempt/fence/plan_revision.
3. `bind_stage_receipt(..., is_separate_actor_author=False)` refuses to author a receipt for a
   stage whose `isolation_requirement` is `separate-actor` unless the caller explicitly asserts a
   distinct actor produced it.
4. Not enforced at the type level in this issue (no mutable instance object is carried across
   calls); the manifest schema's `additionalProperties: false` plus the `stage_id`/`role_id`
   fields being part of `AGENT_INSTANCE_SCHEMA`'s required set means any code path that tries to
   swap them post-`ready` fails schema validation on write.
5. Retry is the caller's responsibility (this module is pure/no I/O); `agent_instance_id` +
   `attempt_id` are both present so a caller cannot silently reuse the previous attempt's identity.
6. All schemas require explicit `input_hash`/`output_hash`/`context_hash`/`manifest_hash`/
   `integrity_hash` fields — content-addressing is structural, not optional.
7. Every schema declares `additionalProperties: false`; unknown fields fail closed
   (`StageAgentError(reason_code="schema_violation")`).
8. `optional_when` is a plain string field on `StageDefinition` — its *evaluation* (was the
   condition true, is it recorded on a receipt) is left to the stage-input/receipt payload; this
   issue defines the slot, not the evaluator, per its "normative foundation" scope.
9. `StageGraphState` is a pure reducer: `validate_manifest()` rejects cycles/orphans/unknown deps
   at load time (`StageAgentError(reason_code="cycle_detected"/"unknown_dependency"/"unknown_role")`);
   at runtime, `apply_receipt()` only accepts a stage receipt whose `depends_on` are already in
   `passed_stages`, rejecting the rest as `dependency_skip`.
10. `classify_receipt_schema()` maps the legacy `simplicio.agent-receipt/v1` schema to
    `"legacy-unbound"` — readable, but `StageGraphState.apply_receipt()` only ever accepts
    `simplicio.stage-receipt/v1` receipts, so a legacy receipt can never promote a stage to
    terminal.

## CLI

```
python3 scripts/stage_agents.py validate                       # validate contracts/stage-agents/v1/stages.json
python3 scripts/stage_agents.py validate --fixture <f> --schema <schema-id>
python3 scripts/stage_agents.py graph --run-id <r> --task-id <t>
python3 scripts/stage_agents.py receipt --fixture <f>
python3 scripts/stage_agents.py status --run-id <r> --task-id <t> --receipts-dir <dir>
python3 scripts/stage_agents.py selftest
```

Every subcommand emits JSON on stdout and returns a non-zero exit with a stable `reason_code` on
any violation — see `contracts/stage-agents/v1/fixtures/` for a complete, receipt-gated DAG
fixture set (`stage_receipt.{coordinate,implement,review,test,integrate}.valid.json`) that
`scripts/stage_agents.py status` walks end to end to `terminal_reached: true`, and
`tests/test_stage_agent_contract.py` proves that removing or adulterating any one receipt in that
set blocks the terminal state (the issue's Definition of Done).

## Fixtures

`contracts/stage-agents/v1/fixtures/` contains one valid instance per schema plus a legacy-receipt
sample (`stage_receipt.legacy.json`) used to prove invariant 10. `tests/test_stage_agent_contract.py`
also exercises invalid variants generated in-memory (missing required field, unknown enum, unknown
field, cycle, unknown dependency, stale fence/revision) rather than shipping a separate `*.invalid.json`
file per case, to avoid an unbounded fixture fan-out for what pytest already parametrizes cleanly.
