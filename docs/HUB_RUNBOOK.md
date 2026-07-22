# Simplicio Loop Hub local IPC runbook

The Hub is opt-in. Existing callers remain standalone unless they explicitly connect to a Hub
endpoint.

## Transport and security

- POSIX uses a Unix domain socket with mode `0600`.
- Windows uses a named pipe (`AF_PIPE`) through the Python standard library.
- TCP is not selected implicitly; a future TCP fallback must be explicit and authenticated.
- Requests use `simplicio.hub-ipc/v1` and are rejected when schema or version differs.
- The lock contains the owner PID. A live owner blocks a second daemon; a dead or corrupt lock is
  reclaimed deterministically.

## Lifecycle

```powershell
simplicio-hub serve --lock <path> --endpoint <endpoint>
simplicio-hub doctor --lock <path> --endpoint <endpoint>
```

`doctor` performs a real `ping` over the selected endpoint and reports lock ownership and
reachability. `HubSocketServer.shutdown()` is idempotent and removes the Unix socket and singleton
lock. If the Hub is unavailable, callers must keep using their standalone adapter.

## Supervisor execution

The versioned IPC method `execute` accepts a `process_spec` using
`simplicio.process-spec/v1` and returns a bounded `simplicio.process-result/v1`. The payload is
argv-only: `shell=true`, unknown fields, invalid cwd roots, and environment keys outside the
allowlist are rejected. The Hub invokes the compiled Rust/Tokio adapter when it is available and
reports `backend: "rust"`; otherwise it deliberately uses the safe Python adapter and reports
`backend: "python-fallback"`. The fallback preserves standalone compatibility but does not claim
Rust-level cross-platform resource controls. cgroups, Windows Job Objects, quotas, and full Hub
queue integration remain separate supervisor work.

### Stage-agent provider

`HubQueueAgentClient` is the public bridge from `QueueAgentAdapter` to the Hub. It accepts an argv-only, absolute-cwd `ProcessSpec` from the stage context and propagates
run/stage/agent/process identity, deadline, idempotency key, priority (`test` for validation/review
stages, otherwise `build`), and the Hub governor resource request. The provider never starts a subprocess, thread, or supervisor. It uses the dedicated
`hub_agent_claim/send/status/collect/cancel` lifecycle; the Hub-owned durable executor is the only
process authority. Claims include an argv-only `ProcessSpec` plus the resource request, while every
later mutation carries the complete fenced Hub handle.

Every mutating IPC effect has a fsynced hash-chain intent/effect journal record. Recovery observes
the existing durable handle and never redispatches it; a process from a previous Hub epoch is
reported as `recovery_unknown` rather than guessed successful or automatically repeated. Use
`StageAgentCoordinator(..., strict_hub=True)` with
`QueueAgentAdapter(queue_client=HubQueueAgentClient(...))` to fail closed when the Hub is absent;
strict mode never falls back to `CommandAgentAdapter`.

The executable conformance/system lane is `tests/test_hub_queue_agent_client.py`. It covers fake
and real Hub transport, heartbeat/result/cancel, timeout/OOM/truncation classifications, stale
fences, restart replay, and an AST architecture gate forbidding subprocess calls in the provider.
Run the real transport/process benchmark with
`python3 scripts/benchmark_hub_queue_agent_client.py --iterations 20 --output
bench/hub-queue-agent-client-baseline.json`. It starts the real Unix Hub socket, executes real Python
processes, and retains raw prepare/claim, send-to-terminal, collect, and total latency samples. Peak
RSS is `null` with an explicit reason on platforms where it cannot be measured; it is never invented.

## Fair scheduler (DRR/quotas, #505)

`HubDaemon` always dispatches through a `FairScheduler` (`simplicio_loop/hub_scheduler.py`):
deficit round-robin per `client_id`, hierarchical queue quotas (global/workspace/client), and
tick-based aging. `submit` enqueues into it; `claim_next` pops in fairness order (not FIFO);
`cancel`/`result` release the client's inflight/quota slot; `scheduler_status` returns the live
state.

### Priority classes (step 1 of #505)

Every `ScheduledJob` carries a `priority` field, one of the seven classes named in the issue —
`interactive`, `mapping`, `llm`, `test`, `build`, `background`, `maintenance` — each mapped to a
deficit-gain multiplier in `PRIORITY_GAIN_MULTIPLIER` (`hub_scheduler.py`): `interactive=8x` down
to `maintenance=0.5x`. `background` (the default, `1x`) preserves the exact
`gain = quantum * weight` formula every pre-priority call site already relied on, so omitting
`priority` on `submit` is a no-op. `enqueue()` fails closed (`SchedulerError`) on an unrecognized
priority string instead of silently defaulting it. Over IPC, pass `priority` in the `submit`
payload; the daemon forwards it to `FairScheduler.enqueue()` verbatim.

Within the same DRR pass, a higher-priority job's larger gain multiplier lets it clear its cost
threshold sooner than a same-weight lower-priority job queued in the same client/tick window — it
does not bypass hierarchical quotas or per-client inflight caps, which are enforced independently
of priority.

### Jain's fairness index (metrics reproduce the decision, #505 AC)

`status()` now reports `served_total` (cumulative dispatch count per client) and
`jains_fairness_index` natively — `(sum(served)^2) / (n * sum(served^2))` over all known clients,
`1.0` when no client has been served yet (including an empty scheduler). Previously this was only
computed ad hoc inside test code; any caller — including a future daemon status endpoint — can now
read the live fairness number instead of recomputing it externally from raw per-client counters.

### Benchmark

`scripts/benchmark_hub_scheduler.py` drains a heavy (default 500 jobs) + light (default 100 jobs)
backlog through a real `FairScheduler` and reports throughput, dispatch p50/p95 latency, the native
`jains_fairness_index`, `starvation_preventions`, and peak RSS (`resource.getrusage`, POSIX only —
`rss_source: "unavailable"` elsewhere, never a fabricated number). Baseline committed at
`bench/hub-scheduler-baseline.json`; regenerate with
`python3 scripts/benchmark_hub_scheduler.py --output bench/hub-scheduler-baseline.json` after a
deliberate scheduler change.

### Failure modes in the current code

- **Backpressure storms**: a client at its `max_queue_per_client`/`max_queue_per_workspace`/
  `max_global_queue` limit gets `HubBackpressureError` on every `submit` (see
  `QuotaExceededError.to_backpressure_signal()`), not a silent drop. If quotas are set too low
  for real traffic, every caller of that scope sees rejected submits until jobs complete/cancel
  and slots are released in `_release_slot`.
- **Misconfigured aging**: `aging_ticks`/`aging_boost` only kick in once a client has waited more
  than `aging_ticks` scheduler ticks without being served (`FairScheduler.next()`). A very high
  `aging_ticks` with a heavy, high-weight client can let a light client wait a long time before
  the boost engages — this is a tuning knob, not a bug, but it is the concrete lever if a client
  reports being starved.
- **In-memory-only state**: `self.scheduler` lives in the daemon process. A daemon restart
  (`HubDaemon.stop()`/process exit) resets all deficits, inflight counts, and queued
  `ScheduledJob`s to empty — jobs already durable in `HubRetryQueue` are not lost, but their
  scheduler position (queue order, accumulated deficit) is. This is documented behavior, not a
  regression: `claim_next` in `HubDaemon` intentionally does not call
  `HubRetryQueue.sync_fair_scheduler()`/`claim_fair()` (see the docstring on
  `sync_fair_scheduler` in `hub_queue_retry.py`) because the daemon's `state` column never leaves
  `'queued'` outside the payload JSON, so doing so would re-enqueue every historical job on every
  restart.
- **Stale scheduler entries**: `_claim_next_locked` defensively retires (`_retire_scheduler_entry`)
  any scheduler entry whose `task_id` is no longer a live queued row in `HubRetryQueue` — this can
  only happen if something mutated the durable row's `state` without going through
  `scheduler.cancel()`/`scheduler.complete()`; it fails safe (skip and keep looking) rather than
  re-serving a finished job, but repeated occurrences indicate the two stores have drifted.

### Detecting degradation

- Call `scheduler_status` over IPC (or `daemon.scheduler.status()` in-process). Key fields:
  `queued`, `inflight` (per client), `deficit` (per client), `starvation_preventions` (cumulative
  aging-boost activations — a steadily climbing counter for one client means it is regularly
  waiting past `aging_ticks` and should be investigated), `client_total`/`workspace_total`/
  `global_total` (live quota usage), `tick`, `served_total` (per-client cumulative dispatch count)
  and `jains_fairness_index` (Jain's index over raw `served_total`, `1.0` = perfectly fair;
  trending toward `1/n` signals starvation of some clients — it is not weight-normalized, so
  clients that legitimately submitted different job counts will show a lower index without that
  being unfairness).
- A caller seeing repeated `HubBackpressureError` with the same `scope`/`client_id`/`workspace_id`
  in `to_backpressure_signal()` is at that scope's quota, not being throttled arbitrarily — check
  `current` vs `limit` in the signal.

## Scheduler rollout and rollback (#635)

The durable scheduler manifest is changed through the versioned `scheduler_configure` IPC method.
Modes are fail-closed: `off` dispatches jobs pinned to `previous_version`, `shadow` keeps that same
single authority while recording the candidate policy, `canary` pins a deterministic percentage of
new jobs, and `on` pins all new jobs to `version`. The response is a rollout receipt whose
`dispatch_authorities` is always `1`; shadow never submits or claims a second copy.

Example rollout: `scheduler_configure` with `{"mode":"shadow","version":"fair-drr-v3",
"previous_version":"fair-drr-v2","canary_percent":0}`, then canary at 5, then `on`. Roll back by
sending `off` with the same two versions. The manifest is committed in the queue SQLite WAL before
it is exposed in memory. Every submitted row retains its `scheduler_policy` pin, so rollback does
not rewrite, lose, or reinterpret jobs and receipts already persisted. Restart reloads the manifest
and rehydrates each job with its original pin.

Limits: rollout changes ordering only for newly submitted jobs; it deliberately does not migrate
queued policy pins or reset accumulated deficits. SQLite/WAL remains the sole durable authority,
and the daemon remains the sole dispatch authority. Invalid modes, empty versions, and canary
percentages outside 1..99 are rejected without replacing the last manifest. If AF_UNIX is absent,
the multiprocess system lane reports `reason_code=af_unix_unavailable` rather than claiming a pass.

### Reproducible multiprocess and performance evidence

Run `python -m pytest -q tests/test_hub_scheduler_multiprocess_system.py` for real spawned producer
and consumer processes over the real Hub socket and SQLite WAL. It uses barriers/events rather
than sleeps. Run `python scripts/benchmark_hub_scheduler.py --heavy-jobs 500 --light-jobs 100
--output bench/hub-scheduler-baseline.json` for the raw environment/commit, throughput, Jain,
queue-wait p95/p99, dispatch p95/p99, RSS, and backpressure baseline.
