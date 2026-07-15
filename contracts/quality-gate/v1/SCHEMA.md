# `simplicio.quality-matrix/v1` — the Quality Gate receipt (#283)

This directory documents the JSON contract the loop's **Quality Gate** reads to decide
whether a work item may ever be reported `COMPLETE`. It is the schema referenced by
issue #283 ("Implementar Quality Gate obrigatório com TDD, testes completos, cobertura
mínima e benchmark") and matches the real implementation, not an aspirational spec:

* implementation: `simplicio_loop/quality_matrix.py` (`evaluate_quality_matrix`,
  `build_quality_matrix_template`, `classify_change_type`, `default_policy_for_change_type`)
* CLI: `scripts/quality_matrix.py` (`build` / `check` / `classify` / `selftest`)
* consumer: `simplicio_loop/oracle.py::evaluate_completion` — the completion oracle
  refuses `COMPLETE` unless this receipt evaluates `ready: true` (issue #278, extended
  here). There is no code path that marks a work item done while skipping this gate.
* schema: [`schema.json`](./schema.json) (JSON Schema draft-07)

## Baseline (#278) — always enforced

Every receipt must supply `requirements.implementation`, `.unit`, `.integration`,
`.system`, `.regression`, and `.benchmark`, each `{"status": "pass", "proof_ref": "<non-empty>"}`,
plus `coverage.measured >= coverage_threshold` (default 85%). Any lane missing, failing,
or without a `proof_ref` blocks with a distinct `reason_code` (e.g. `quality_unit_missing`,
`quality_unit_failed`, `quality_unit_unproven`). A missing/unreadable receipt blocks with
`quality_matrix_missing`. This is unconditional and was already wired into the completion
oracle before #283 — #283 does not weaken it.

## #283 additions — strictly additive, opt-in, zero regression

A receipt that omits `policy` entirely, and never sets a `tdd` requirement or a
`not_applicable` status, behaves **exactly** as the #278 baseline above. The additions
below only activate when a receipt explicitly opts in:

### 1. TDD RED → GREEN evidence (`policy.tdd_required`)

Set `policy.tdd_required: true` to require a `requirements.tdd` entry with:

```json
{"status": "pass", "red_proof_ref": "tests/test_x.py::test_feature (failed pre-impl)",
 "green_proof_ref": "tests/test_x.py::test_feature (passed post-impl)"}
```

`red_proof_ref` and `green_proof_ref` must both be non-empty **and different from each
other** — a single ref cannot prove both a failing-before and a passing-after state.
Failure reason codes: `quality_tdd_missing`, `quality_tdd_failed`,
`quality_tdd_red_missing`, `quality_tdd_green_missing`, `quality_tdd_red_green_identical`.

### 2. Justified `NOT_APPLICABLE` benchmark (`policy.allow_justified_not_applicable`)

Only the `benchmark` lane (`NOT_APPLICABLE_ELIGIBLE` in `quality_matrix.py`) may be
excused, and only when **both** `policy.allow_justified_not_applicable: true` **and** a
non-empty `requirements.benchmark.justification` are present:

```json
{"policy": {"allow_justified_not_applicable": true},
 "requirements": {"benchmark": {"status": "not_applicable",
                                 "justification": "no perf-sensitive code path touched"}}}
```

Any other lane set to `not_applicable`, or `benchmark` set to `not_applicable` without
the policy flag or without a justification, blocks with
`quality_<lane>_not_applicable_unjustified`.

### 3. Deterministic change classification

`classify_change_type(title, labels) -> "bug"|"fix"|"feat"|"chore"|"task"` is a
label-authoritative, title-fallback classifier (no LLM call) used to derive a sane
default policy via `default_policy_for_change_type`:

| change_type | `tdd_required` | `allow_justified_not_applicable` |
|---|---|---|
| `feat` / `fix` / `bug` / `task` | `true` | `false` |
| `chore` (docs/refactor/CI, no behavior change) | `false` | `true` |

`scripts/quality_matrix.py build --run-dir <dir> --change-type <type>` seeds a template
with that policy pre-filled; `scripts/quality_matrix.py classify --title "..." --label L`
prints the classification + derived policy standalone.

## Not yet covered by this contract

This receipt format is the fail-closed evidence gate itself. It intentionally does **not**
implement (tracked separately, out of scope for the #283 increment landed here):

* an independent watcher re-deriving each lane's verdict from raw test/CI output rather
  than trusting the receipt's self-reported `status` (the oracle's separate watcher gate,
  `simplicio_loop/oracle.py::_watcher_gate`, already covers response-vs-goal watching, but
  does not yet specifically re-verify quality-matrix lane claims);
* automatic production of the receipt from `scripts/coverage_gate.py` /
  `scripts/regression_test_gate.py` / `scripts/perf_gate.py` / `scripts/quality_matrix_bench.py`
  output (those gates already run in CI, `.github/workflows/quality-gate.yml`, from #277;
  wiring their output into this receipt's `proof_ref`/`coverage.measured` fields
  automatically is follow-up work).
