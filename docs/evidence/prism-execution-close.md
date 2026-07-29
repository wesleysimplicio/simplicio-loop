# Prism execution close evidence (#845–#852, #819, #801)

Honest, measured close lane for the Prism hierarchy already on `main`. This
document maps each open issue to the modules that implement it and the pytest
commands that were run on this branch. No theater acceptance criteria: only
checked-in tests that pass are listed.

## Suite command (all Prism unit/e2e gates)

```bash
python -m pytest \
  tests/test_prism_contracts_845.py \
  tests/test_prism_scheduler_agents_846_847.py \
  tests/test_prism_reducer_recovery_848_849.py \
  tests/test_prism_budgets_850.py \
  tests/test_prism_integrity_851.py \
  tests/test_prism_e2e_852.py \
  -q
```

**Measured on this close branch:** `73 passed` (local, model-free).

Ecosystem floors in `pyproject.toml` at close time:

| Operator | Pin in `pyproject.toml` |
|---|---|
| `simplicio-cli` | `>=0.18.0` |
| `simplicio-mapper` | `>=0.26.0` |
| `simplicio-fast` | `>=2.0.16` |
| Loop package | `3.38.8` (`requires-python >=3.11`) |

Operator product docs: `docs/PRISM_EXECUTION.md`, `docs/PRISM_BENCHMARK_852.md`,
`docs/PRISM_COMPATIBILITY.md`. Contracts under
`simplicio_loop/_contracts/prism/v1/`.

---

## #845 — [P0][Prism Contract] PrismExecution/v1, SlotSupervisor/v1, TaskOwnership/v1

| Field | Value |
|---|---|
| Module | `simplicio_loop/prism_contracts.py` |
| Schemas | `simplicio_loop/_contracts/prism/v1/` (HBP golden + conformance cases) |
| Tests | `tests/test_prism_contracts_845.py` |

```bash
python -m pytest tests/test_prism_contracts_845.py -q
```

What the suite measures (not stubs):

- Content-addressed IDs are permutation-invariant.
- Slot admits 10 tasks; 11th is queued/rejected with `SLOT_LOGICAL_CAPACITY` (never silent widen).
- Exactly one accountable owner; hierarchy fails closed on cycles, depth, orphans, duplicates.
- Transitions require current owner + fence + allowed state.
- HBP frame round-trip; adversarial/unknown fields reject without widening authority.
- Checked-in HBP golden is byte-exact; legacy envelopes are readable but never authoritative.

---

## #846 — [P0][Prism Scheduler] Recursive slots (≤10 tasks) + adaptive waves

| Field | Value |
|---|---|
| Module | `simplicio_loop/prism_scheduler.py` |
| Tests | `tests/test_prism_scheduler_agents_846_847.py` (scheduler half of the file) |

```bash
python -m pytest tests/test_prism_scheduler_agents_846_847.py -q -k "slot_admission or dependencies_conflicts or global_budget or provider_retry or async_execution or cancel_parent or scheduler_validation or slot_and_submission or execute_failure or cancel_running"
```

What the suite measures:

- Slot admission never exceeds ten; eleventh is queued with a reason code.
- Dependencies/conflicts/exclusive resources serialize only the affected group.
- Global budget + missing metrics stay conservative (no oversubscribe).
- Provider `RETRY_AFTER`, pressure, and reserved capacity have explicit reasons.
- Async execution shows real temporal overlap and no lost tasks.
- Cancel parent cancels children; snapshot replay is bounded.

---

## #847 — [P0][Prism Agents] Real accountable agent per transition

| Field | Value |
|---|---|
| Module | `simplicio_loop/prism_agents.py` |
| Tests | `tests/test_prism_scheduler_agents_846_847.py` (agent half of the file) |

```bash
python -m pytest tests/test_prism_scheduler_agents_846_847.py -q -k "agent_assignment or review_and_completion or agent_mailboxes or policy_resource or controller_all or agent_contract or agent_missing"
```

What the suite measures:

- Assignment independence, heartbeat, takeover, and receipt emission.
- Review and completion agents must be independent from the implementer.
- Mailboxes and expired/missing capability fail closed.
- Registry/policy/task/observation validation boundaries.
- Controller denial and lifecycle paths; missing assignment TTL/takeover/send boundaries.

Full file (scheduler + agents):

```bash
python -m pytest tests/test_prism_scheduler_agents_846_847.py -q
```

---

## #848 — [P0][Prism Reducer] Causal reconvergence, deterministic integration

| Field | Value |
|---|---|
| Module | `simplicio_loop/prism_reducer.py` |
| Tests | `tests/test_prism_reducer_recovery_848_849.py` (reducer cases) |

```bash
python -m pytest tests/test_prism_reducer_recovery_848_849.py -q -k "reducer"
```

What the suite measures:

- Reducer is permutation-invariant and never promotes task completion to slot/prism completion without causal closure.
- Missing/tamper/cross-slot/stale/duplicate child receipts are bounded.
- Conflicts, dependencies, child failure, and missing tests fail closed.
- Contract validation boundaries for reducer inputs.

---

## #849 — [P0][Prism Recovery] HBP journal, takeover, recursive recovery without duplicate effect

| Field | Value |
|---|---|
| Module | `simplicio_loop/prism_recovery.py` |
| Tests | `tests/test_prism_reducer_recovery_848_849.py` (recovery/journal cases) |

```bash
python -m pytest tests/test_prism_reducer_recovery_848_849.py -q -k "journal or orphan or recovery or hash_chain or recover_state"
```

What the suite measures:

- Journal replay, checkpoint, and state digest are deterministic.
- Orphan effect intents are reconciled without re-execution (consult existing receipt).
- Corrupt/truncated/partial journals fail closed.
- Recovery never infers terminal success or accepts bad effect order.
- Hash chain and partial-write boundaries fail closed.

Full file (reducer + recovery):

```bash
python -m pytest tests/test_prism_reducer_recovery_848_849.py -q
```

---

## #850 — [P1][Prism Budgets] Adaptive backpressure (slot, global, provider, device)

| Field | Value |
|---|---|
| Module | `simplicio_loop/prism_budgets.py` |
| Tests | `tests/test_prism_budgets_850.py` |

```bash
python -m pytest tests/test_prism_budgets_850.py -q
```

What the suite measures:

- Unknown metrics are conservative and explain `null` with `null_reason`.
- Pressure is immediate; relief uses hysteresis.
- Logical 20×10 never exceeds the configured physical cap.
- Fair-share prevents priority starvation between slots.
- Provider retry, exclusive, and reserved capacity remain bounded.
- Device loss increments fence without duplicate work; loss without target requires recovery, not re-execution.
- Throughput receipts never invent cost/token metrics.

---

## #851 — [P0][Integrity] Python floor, versions, installable Prism core closure

| Field | Value |
|---|---|
| Script | `scripts/prism_integrity.py` (`evaluate`) |
| Tests | `tests/test_prism_integrity_851.py` |

```bash
python -m pytest tests/test_prism_integrity_851.py -q
```

What the suite measures:

- Live repository metadata and pins are coherent (`ok is True`, Python minimum `3.11`).
- Python floor drift is blocked (`PYTHON_FLOOR_DRIFT`).
- Dependency floor and Fast submodule branch drift are blocked
  (`DEPENDENCY_FLOOR_DRIFT`, `SUBMODULE_PIN_DRIFT`) against floors
  `simplicio-cli>=0.18.0`, `simplicio-mapper>=0.26.0`, `simplicio-fast>=2.0.16`.
- Package version surface drift is blocked (`VERSION_SURFACE_DRIFT`).

---

## #852 — [P0][Prism E2E] Prove 1×10, 4×10, 20×10 with reproducible benchmark

| Field | Value |
|---|---|
| Benchmark | `bench/prism_benchmark_852.py` |
| Results (optional raw) | `bench/results/prism-benchmark-852.json` |
| Docs | `docs/PRISM_BENCHMARK_852.md` |
| Tests | `tests/test_prism_e2e_852.py` |

```bash
python -m pytest tests/test_prism_e2e_852.py -q
```

Optional measured benchmark (same harness the tests call):

```bash
python bench/prism_benchmark_852.py \
  --repetitions 10 \
  --physical-cap 20 \
  --output bench/results/prism-benchmark-852.json
```

What the suite measures:

- Loads `{1x10, 4x10, 20x10}` with `measurement == "measured"` and `projection is False`.
- S0 serial, S1 legacy, S2 Prism Python, S4 Python fallback correctness oracles.
- S3 Runtime Rust remains `measured=false` with `RUNTIME_BINARY_NOT_FOUND` when no protocol binary is present (honest null, not inventing parity).
- No lost tasks / no duplicate invocations; max temporal overlap ≤ physical cap.
- Methodology rejects invalid repetitions/cap/delay parameters.

---

## #819 — [EPIC][P0][Prism Execution] 1 Slot Supervisor = up to 10 issues/tasks

Parent epic for the hierarchy Goal → PrismExecution → SlotSupervisor (≤10) →
agents → reducer/oracle. Closed by the leaf delivery of #845–#852 plus product
docs (`docs/PRISM_EXECUTION.md`).

Verification is the full Prism suite above (73 tests) plus the contract
admission invariant: eleventh task is never silently admitted.

```bash
python -m pytest tests/test_prism_*.py -q
```

---

## #801 — [EPIC][P0] Ultrarrápido/econômico: Mapper + Fast + Dev CLI inside Loop

Foundation epic for the operator stack consumed by Prism integrity. Close
evidence is the installable dependency floors and integrity gate (not a
reimplementation of Mapper/Fast/Dev CLI):

```bash
python -m pytest tests/test_prism_integrity_851.py -q
```

Pins verified in `pyproject.toml` dependencies:

- `simplicio-cli>=0.18.0`
- `simplicio-mapper>=0.26.0`
- `simplicio-fast>=2.0.16`

Loop version surface: `3.38.8`.

---

## What this PR does / does not do

**Does:** Document measured commands and module mapping so GitHub can close the
already-implemented Prism issues with auditable evidence.

**Does not:** Re-implement Prism modules, invent new tests, claim Runtime Rust
parity (S3) when the binary/protocol is absent, or invent cost/token metrics.

If any of the listed pytest commands fail, do **not** close the corresponding
issue until the failure is fixed and re-measured.
