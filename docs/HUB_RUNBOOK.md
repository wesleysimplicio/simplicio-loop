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

## Fair scheduler (DRR/quotas, #505)

`HubDaemon` always dispatches through a `FairScheduler` (`simplicio_loop/hub_scheduler.py`):
deficit round-robin per `client_id`, hierarchical queue quotas (global/workspace/client), and
tick-based aging. `submit` enqueues into it; `claim_next` pops in fairness order (not FIFO);
`cancel`/`result` release the client's inflight/quota slot; `scheduler_status` returns the live
state.

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
  `global_total` (live quota usage), `tick`.
- A caller seeing repeated `HubBackpressureError` with the same `scope`/`client_id`/`workspace_id`
  in `to_backpressure_signal()` is at that scope's quota, not being throttled arbitrarily — check
  `current` vs `limit` in the signal.

### Rollback

There is no runtime env var or CLI flag to disable the fair scheduler in the current code —
`HubDaemon.__init__` always constructs a `FairScheduler()` with its class defaults
(`max_inflight_per_client=4`, `quantum=1`, no queue quotas, `aging_ticks=20`, `aging_boost=4`) when
no `scheduler=` argument is passed. Two real levers exist today, from weakest to strongest:

1. **Neutralize quotas without a code change**: construct the daemon with
   `HubDaemon(lock_path, scheduler=FairScheduler(max_queue_per_client=10**9,
   max_queue_per_workspace=10**9, max_global_queue=10**9, max_inflight_per_client=10**9))` to stop
   `QuotaExceededError`/backpressure from firing while keeping DRR ordering.
2. **Full rollback**: revert the commits that introduced `FairScheduler` wiring in
   `simplicio_loop/hub_daemon.py` (the `scheduler`/`claim_next`/`scheduler_status` code paths) and
   redeploy — the daemon and `HubRetryQueue` both work standalone without it, since `claim`/
   `submit`/`cancel`/`result` do not otherwise depend on the scheduler object.

Stop the daemon, remove only the stale lock after confirming its PID is dead, and unset the Hub
endpoint/feature flag in the caller. No job state is treated as delivered until the caller receives
the versioned response envelope.
