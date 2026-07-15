# ADR-0004 — Planning-gate rollout: `SIMPLICIO_LOOP_AUTO_PLANNING_RECEIPT` mandatory-by-default

- **Status:** accepted
- **Date:** 2026-07-15
- **Relates to:** issue #284 (task-intake contract + executable plan barrier), PR #360
  (`SIMPLICIO_REQUIRE_MUTATION_AUTHORITY` mandatory-by-default flip — the precedent this ADR
  follows), PR #376 (`_maybe_auto_build_planning_receipt()` wired opt-in into the real
  `arm_run()` dispatch path).

## Context

Issue #284's Definition of Done requires that "nenhum claim de 'planejamento profundo
obrigatório' permanece apenas em prompt/documentação" — no claim of mandatory deep planning may
remain purely descriptive. Two gates exist on the `claim → plan → execute` boundary:

1. **`SIMPLICIO_REQUIRE_MUTATION_AUTHORITY`** (`simplicio_loop/planning_gate.py
   ::mutation_authority_required()`), gating `execute_operator()`/`execute_operator_batch()`:
   refuses mutation without a valid `planning-receipt.json` whose `mutation_authority` token
   matches the current run/attempt/contract/plan/lease/fence identity. PR #360 already flipped
   this to mandatory-by-default (unset/blank → required; only an explicit falsy value opts
   out).
2. **`SIMPLICIO_LOOP_AUTO_PLANNING_RECEIPT`** (`simplicio_loop/runner.py
   ::_maybe_auto_build_planning_receipt()`), wired into the real `arm_run()` dispatch path: it
   is what actually *builds* the `planning-receipt.json` gate (1) checks for. Until this
   change it remained opt-in (default OFF), which meant gate (1) being "mandatory" only forced
   callers to have *some* receipt on disk — it did not force the real dispatch path to ever
   produce one itself. A caller unaware of the separate `scripts/planning_gate.py build` CLI
   (or the `stage_valid_planning_receipt()` test fixture) could still run `arm_run()` to
   completion with no receipt materializing, then hit the mandatory gate as an opaque runtime
   failure with no self-service remedy wired into the same code path.

This left the#284 chain incomplete: the receipt was mandatory to *check*, but not mandatory to
*produce*. This ADR closes that gap by flipping gate (2)'s polarity the same way PR #360 flipped
gate (1)'s.

## Decision

Flip `SIMPLICIO_LOOP_AUTO_PLANNING_RECEIPT` to mandatory-by-default, using the exact pattern
`mutation_authority_required()` established:

```python
def auto_planning_receipt_enabled(env=None) -> bool:
    raw = (env if env is not None else os.environ).get("SIMPLICIO_LOOP_AUTO_PLANNING_RECEIPT")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "legacy")
```

- Unset/blank → **True** (auto-build ON). Every real `arm_run()` dispatch now self-builds a
  matching `planning-receipt.json` immediately after the plan is materialized and validated, so
  `execute_operator()`/`execute_operator_batch()` are self-sufficient by default instead of only
  ever being satisfiable by a caller remembering a separate CLI step.
- Only an explicit falsy value (`0`/`false`/`no`/`off`/`legacy`, case-insensitive) restores the
  pre-flip (opt-in-required) behavior. Any other unrecognized non-empty value is treated as "on"
  — the same fail-closed-toward-safety posture as `mutation_authority_required()`, so a typo
  (e.g. `SIMPLICIO_LOOP_AUTO_PLANNING_RECEIPT=nope`) cannot silently disable the gate.
- Same name kept (only the default polarity changes), matching the precedent set for
  `SIMPLICIO_REQUIRE_MUTATION_AUTHORITY` in PR #360 — existing explicit `=1` deployments are
  unaffected; only callers that previously relied on the *implicit* unset-means-off behavior
  need to add an explicit opt-out.

### Backward-compatibility shims

- **Best-effort, fail-open build**: `_maybe_auto_build_planning_receipt()` was already
  best-effort (any failure — bad `gh` auth, missing GitHub source, import error — is logged to
  `lifecycle-sync-errors.jsonl` and swallowed, never aborts the run). This property is
  unchanged by the polarity flip: turning the auto-build on-by-default cannot newly fail a run
  that previously succeeded without a receipt, because the receipt build itself cannot raise
  into the caller.
- **GitHub publish stays behind its own flag.** Auto-building the local
  `planning-receipt.json` is now unconditional; publishing it to a live GitHub issue comment
  still requires `SIMPLICIO_LOOP_GITHUB_LIFECYCLE_SYNC=1` separately. A caller with no GitHub
  source configured, or that has not opted into lifecycle sync, gets a receipt on disk and zero
  network calls — identical to before this change for that half of the behavior.
- **Test fixtures**: `tests/planning_gate_fixtures.py::stage_valid_planning_receipt()` remains
  available for tests that want to construct a receipt with specific (e.g. tampered or
  cross-run) content rather than the one `arm_run()` would auto-build. Tests asserting the
  legacy fail-closed "no receipt exists" path now do so via an explicit
  `SIMPLICIO_LOOP_AUTO_PLANNING_RECEIPT=0`, mirroring how mutation-authority tests opt out of
  gate (1) — see `tests/test_284_lifecycle_dag_idempotence.py
  ::test_arm_run_does_not_auto_build_receipt_when_explicitly_disabled`.

### What breaks

- Any caller or test that previously called `arm_run()` and asserted
  **`planning-receipt.json` does not exist** with the flag left unset will now see that
  assertion fail — the receipt is built by default. The one such test in this repo
  (`test_arm_run_does_not_auto_build_receipt_by_default`) was updated to set the flag to `0`
  explicitly and renamed to `test_arm_run_does_not_auto_build_receipt_when_explicitly_disabled`.
- No other call sites in `simplicio_loop/` or `scripts/` read this env var; the mandatory gate
  (`mutation_authority_required()`) was already unconditionally on, so downstream behavior for
  a caller that previously supplied its own receipt (via `stage_valid_planning_receipt()` or the
  `scripts/planning_gate.py build` CLI) is unchanged — the auto-build simply becomes redundant
  (and is a no-op once a valid receipt for the current identity is already on disk, since
  `build_planning_receipt()` writes deterministically over the same file).

### How to opt out (temporarily)

A legacy caller or CI job that cannot yet satisfy the gate should set:

```bash
export SIMPLICIO_LOOP_AUTO_PLANNING_RECEIPT=0
```

This restores byte-for-byte the pre-flip behavior: `arm_run()` materializes `plan.json` and
`mapper-context.json` as before but never writes `planning-receipt.json`, and any caller of
`execute_operator()`/`execute_operator_batch()` downstream must still satisfy
`mutation_authority_required()` some other way (an explicit `scripts/planning_gate.py build`
call, `stage_valid_planning_receipt()` in tests, or its own falsy
`SIMPLICIO_REQUIRE_MUTATION_AUTHORITY`). The two flags are independent; disabling auto-build
does not by itself disable the mutation-authority requirement.

## Consequences

- Every real `arm_run()` invocation now writes a `planning-receipt.json` into the run directory
  by default — a new on-disk artifact for any consumer that walks a run directory expecting the
  pre-#284 file set. It is additive (new file, no existing file's shape changes).
- Issue #284's DoD item "nenhum claim de 'planejamento profundo obrigatório' permanece apenas em
  prompt/documentação" is now true for the auto-build half of the gate: a caller that never
  reads any documentation still gets a self-built receipt from the real dispatch path, not only
  from a caller that remembered the separate CLI.
- This ADR does not implement the still-open #284 gaps: the full `simplicio.task-intake/v1`
  envelope (scope in/out, dependencies, risks, rollback), `impact-map.json`, the bidirectional
  AC↔step↔test↔evidence matrix as a persisted artifact, wiring GitHub `CLAIMED`/`PLANNED`
  comment publish+re-query into the mandatory path itself (today it is opt-in via
  `SIMPLICIO_LOOP_GITHUB_LIFECYCLE_SYNC`), and full replanning-on-drift wired automatically
  into the dispatch path (`replan_on_drift()` exists and is tested, but nothing calls it
  automatically yet). Issue #284 remains open after this change.
