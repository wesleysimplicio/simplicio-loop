# Supervisor enforcement runbook + threat model (issue #516)

Parent epic: [#498](https://github.com/wesleysimplicio/simplicio-loop/issues/498) — one
Process Supervisor for every process the Simplicio ecosystem starts. This doc covers
[#516](https://github.com/wesleysimplicio/simplicio-loop/issues/516): detection of
out-of-supervisor processes, the `status`/`top`/`queue`/`cancel`/`drain`/`reports` CLI
surface, opt-in enforcement, and a circuit breaker with standalone fallback.

**Scope note, up front:** this is the first real slice, not the full #498 DoD. It is
built on the already-merged [#514](https://github.com/wesleysimplicio/simplicio-loop/issues/514)
Python `ProcessSpec`/`ProcessLease`/`ProcessResult` contract
(`simplicio_loop/process_supervisor.py`) and works standalone with the Python adapter —
it does **not** assume the Rust/Tokio backend from
[#515](https://github.com/wesleysimplicio/simplicio-loop/issues/515) exists. See
"Implemented now vs. deferred" below for an honest line between what this slice proves
and what the epic still needs.

## What's implemented now

| Capability | Module | Real, tested behavior |
|---|---|---|
| Detector | `simplicio_loop/process_enforcement.py` — `scan_host_processes`, `is_simplicio_cmdline`, `detect_unsupervised` | Enumerates live OS processes (`/proc/<pid>/cmdline` on Linux, `ps -axo pid=,args=` fallback elsewhere on POSIX) and flags any whose argv matches a Simplicio-ecosystem signature (`SIMPLICIO_SIGNATURES`) but whose pid is not in the supervisor's own bookkeeping (`ProcessRegistry`). |
| Bookkeeping | `ProcessRegistry` | A JSON file (default `.orchestrator/supervisor/registry.json`) recording pid → lease_id/spec_hash/argv for every process currently running through `SupervisedProcessAdapter`. Persisted so a separate CLI invocation can read it. Stale entries (pid reused after a crash) are pruned via `os.kill(pid, 0)` liveness probing on every read. |
| Supervised spawn | `SupervisedProcessAdapter` | Wraps the #514 `PythonProcessAdapter`. Uses a new, additive `on_spawned` hook on `PythonProcessAdapter.run()` (backward compatible — default `None`, existing #514 tests unchanged) to register the real OS pid the instant it's known, and unregisters it when the process ends. |
| CLI | `simplicio_loop/process_enforcement_cli.py` (`python -m simplicio_loop.process_enforcement_cli ...`) | `status` (enforcement mode + active count + breaker state), `top` (active supervised processes with pid/lease/age), `queue` (in-flight leases — see caveat below), `cancel --pid/--lease-id` (real SIGTERM), `drain --timeout [--force]` (waits for leases to finish; `--force` SIGTERMs stragglers), `reports [--scan] [--limit]` (replay a JSONL event log, or run + log a fresh detection pass). |
| Enforcement mode | `enforcement_enabled()` / `enforce()` | **Opt-in, default OFF** (`SIMPLICIO_SUPERVISOR_ENFORCE=1` to turn on). Off: `enforce()` only reports what it *would* do (`action: "observed_only"`), never sends a signal — proven by a test that spawns a real flagged process and asserts it is still alive afterward. On: `enforce()` sends a real `SIGTERM` to flagged pids — proven by a test that spawns a real flagged process and asserts it is actually gone afterward. |
| Circuit breaker | `CircuitBreaker` | Trips **OPEN** after `failure_threshold` (default 3) *consecutive* supervised-spawn failures classified as `spawn_error`/`executable_not_found` (not ordinary non-zero exit codes from the user's own command — that's a documented, narrow trip condition, not "anything failed"). Moves to `half_open` after `cooldown_seconds`; a subsequent success closes it. State persists to disk (`breaker.json`) so the CLI can report it across invocations. |
| Fallback | `run_guarded()` | When the breaker is OPEN, subsequent work runs through a **plain, unsupervised** `PythonProcessAdapter` (still argv-only/spec-validated, just not registered) instead of being refused — proven by a test where, after two forced failures trip the breaker, a *good* spec still executes successfully via `mode: "standalone_fallback"`. |

All of the above is exercised by `tests/test_process_enforcement.py` with **real
subprocesses** (never mocked): a canary spawned outside the supervisor is shown to be
flagged; the same kind of process spawned through `SupervisedProcessAdapter` is shown
NOT to be flagged while it's still running; enforcement-off is shown to leave a flagged
process alive; enforcement-on is shown to actually terminate one; the breaker is shown
to trip and to still complete new work via fallback; the CLI subcommands are exercised
end-to-end as a real subprocess against a real registry file.

```bash
python3 -m pytest tests/test_process_enforcement.py -v
python3 -m pytest tests/test_process_supervisor_spec.py tests/test_async_io_supervisor.py -v  # no regression on #514
```

## Threat model

**Assets protected:** host resources (CPU/RAM/disk/process table) and the integrity of
the supervisor's own bookkeeping.

**In scope for this slice:**

- *A Simplicio-ecosystem command bypasses the supervisor entirely* (an IDE, a shell
  alias, a stale script invoking a Simplicio CLI directly). Mitigated by detection
  (observability today; termination only when the operator opts in via
  `SIMPLICIO_SUPERVISOR_ENFORCE=1`/`--enforce`).
- *The supervisor itself is unavailable or crashes mid-run.* The registry is a plain
  file with pid-liveness pruning, not a lock the supervisor process must hold open —
  a crashed supervisor leaves entries that are pruned as soon as anything reads the
  registry and finds the pid gone, per the #498 invariant "failure of the supervisor
  must not leave orphans undetected."
- *An operator wants proof that enforcement is genuinely opt-in.* Both the default
  (`enforcement_enabled()` reads an env var that defaults falsy) and the behavioral
  proof (off ⇒ nothing signaled) are unit-tested, not just documented.

**Explicitly out of scope for this slice (deferred — see below):** privilege
escalation via a spoofed cmdline (an unrelated process could name its argv to *look*
like a Simplicio process and get flagged, or an actual Simplicio process could obscure
its argv to *evade* detection — the signature match is a heuristic, not a
cryptographic attestation); resource-limit enforcement (cgroups/Job Objects); killing
whole process trees (only the direct pid is signaled, not descendants); any
authentication/authorization on who may run `cancel`/`drain`/`--enforce` (this is a
local, single-operator CLI today, same trust boundary as running `kill` yourself).

## What's deferred (and why)

The issue's own AC lists a much larger surface than one slice can honestly close in one
pass. Deferred, with reasons:

- **Rollout shadow/canary automation.** No shadow-vs-canary traffic splitting or
  automated rollout percentage exists yet. This slice gives the primitives an
  automated rollout would gate on (detection + breaker + opt-in enforcement), but the
  rollout *policy* (percentages, promotion/rollback criteria) is a separate piece of
  work once there's a real fleet of supervised workloads to roll out against.
- **Full cross-platform enforcement.** The detector's Linux path (`/proc`) is
  exercised by the test suite on this host. The macOS/other-POSIX `ps` fallback is
  implemented but not exercised here (no macOS runner in this environment). Windows
  process enumeration is explicitly **not implemented** (`scan_host_processes()`
  returns `[]` on `os.name == "nt"`) rather than guessed at — the epic's own test plan
  calls for "Windows Job Objects/Linux cgroups/fallback macOS" stress testing, which is
  real, separate work.
- **Process-tree cancellation.** `cancel`/`drain --force`/`enforce` all signal the
  single registered pid. The #498 invariant "cancellation ends descendants, not just
  the main pid" is not yet satisfied here — that needs a process-group or
  cgroup-based kill, which is exactly the resource-control work items 5–8 of the #498
  plan own.
- **Quotas / fairness / admission control.** Untouched by this slice; those are #498
  items 5–10, owned by other sub-issues.
- **`queue` command depth.** Reports only the registry's *active* (in-flight)
  supervised leases — there is no separate pending-priority queue wired into this
  slice. A real multi-class pending queue is `hub_scheduler.py`/`hub_queue_retry.py`
  territory; wiring `queue` to that is the natural next step (see below).

## Recommended next slice

Wire `SupervisedProcessAdapter`/`ProcessRegistry` into `hub_scheduler.py` (the existing
fair client scheduler) so `queue` reports real pending-vs-active depth per class, and
extend `cancel`/`drain`/`enforce` to signal a process **group** (POSIX
`os.killpg`) instead of a single pid, closing the "descendants" invariant gap noted
above. Do that once #515's Rust backend lands, so the same registry/detector contract
can be validated against both adapters rather than only the Python one.
