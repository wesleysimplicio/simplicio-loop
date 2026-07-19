# Hub resource governor — threat model, detection, and rollback

Covers `simplicio_loop/hub_governor.py` (`ResourceGovernor`, `CircuitBreaker`,
`ResourceProbe`). Scope is this module as it exists today; it does not cover the Rust/Tokio
supervisor, the scheduler, or the queue — those are separate integration points that do not
call into this governor yet (see "Known gaps" below).

## What the governor actually does

`ResourceGovernor.admit()` is a logical, in-process admission gate: it holds integer budgets
(`ResourceLimits`) for a global pool and per `client_id`, and refuses a request
(`ResourceThrottled`) before any lease is recorded when the request would exceed either budget.
`CircuitBreaker` trips after `circuit_threshold` calls to `record_failure()`/
`evaluate_pressure()` and denies all admission (`reason: "circuit_open"`) until
`cooldown_seconds` elapses and a subsequent non-degraded reading closes it (half-open ->
closed). `ResourceProbe.read()` is a best-effort measurement ladder (cgroups v2 -> psutil ->
`resource.getrusage` -> `PressureReading(source="unavailable")`) that never raises.

## Failure modes given the current code

- **Logical budgets, not enforcement.** `ResourceLimits`/`ResourceRequest` are integers the
  caller supplies. Nothing in this module measures real CPU/RSS/disk/GPU against them — a
  caller that requests less than it actually uses is not caught. The governor only protects
  against callers that go through `admit()` honestly.
- **GPU is unmeasured for admission.** `gpu` exists as a budget field, but there is no reader
  wired to it in this file; only CPU/memory pressure feed `evaluate_pressure()`.
- **Probe degrade is silent by design.** If cgroups and psutil are both unavailable,
  `ResourceProbe.read()` returns `source="unavailable"` with all-zero readings and no
  exception. A caller that feeds an unavailable reading into `evaluate_pressure()` without
  checking `reading.source` will never trip the breaker even under real pressure — the module
  cannot distinguish "healthy" from "blind."
- **In-process only, no persistence.** `_usage`, `_leases`, `_receipts`, and the circuit state
  live in one `ResourceGovernor` instance protected by a single `RLock`. A process restart
  loses all counters and receipts; there is no cross-process or cross-host coordination.
- **Idempotent admit by `lease_id`, not by request shape.** Calling `admit()` twice with the
  same `lease_id`/`task_id` but a different `ResourceRequest` silently returns the first lease
  (`admit`, early return at the `lease_key in self._leases` check) — the second request's
  values are discarded, not validated against the first.
- **`drain()`/`shutdown()` are governor-local.** They stop new admissions and (for `shutdown`)
  release all held leases, but do not signal any external process to actually stop running
  work; they only change what this governor will admit next.

## Detecting degradation as an operator

- Call `governor.status()` (schema `simplicio.hub-resource-governor/v1`). Watch `circuit.state`
  (`open` means new admissions are being refused for `circuit.reason`), `active_leases` vs the
  configured limits, and `draining`.
- Call `governor.receipts()` for the full, redacted throttle history (schema
  `simplicio.hub-throttle-receipt/v1`); each receipt has `reason`
  (`draining`/`circuit_open`/`global_budget`/`client_budget`), `resource`, `requested`, and
  `available`. Receipts intentionally never contain `command`, `cwd`, or `env` — do not expect
  those fields when debugging (that redaction is enforced by
  `test_redacted_throttle_receipt`).
- If every `ResourceProbe.read()` call returns `source="unavailable"`, treat pressure-based
  circuit behavior as blind on that host, not as "no pressure" — the reading's `source` field
  is the ground truth for whether measurement is real.

## Rollback

There is no feature flag inside this module — the governor only affects a caller that
constructs a `ResourceGovernor` and calls `admit()`/`evaluate_pressure()` explicitly. To roll
back a caller that has adopted it:

1. Stop calling `admit()` before spawning work (or wrap the call site so a `ResourceThrottled`
   is caught and ignored) — this reverts to unrestricted, standalone behavior.
2. If the circuit is stuck open and blocking legitimate work, call `governor.recover()`
   directly, or construct a fresh `ResourceGovernor` (state is process-local, so a restart of
   the owning process also clears it).
3. To stop draining without a restart, there is no public "undrain" call; `_draining` is only
   ever set to `True`. Recreate the governor instance to resume admissions.

## Known gaps (not implemented in this module)

Real per-OS CPU/RSS/disk/GPU enforcement feeding `admit()`, adaptive concurrency, scheduler
(#505) and queue (#504) integration, and Windows/macOS behavioral verification are not present
in `hub_governor.py` as of this writing — see issue #506's comment thread for the itemized
status of each.
