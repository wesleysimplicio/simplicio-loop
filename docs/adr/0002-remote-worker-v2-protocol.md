# ADR-0002 — `simplicio.remote-worker/v2` protocol: envelope, state machine, and dispatch split

- **Status:** accepted
- **Date:** 2026-07-15
- **Supersedes / relates to:** issue #286 (multi-device protocol), issue #288 (local pipeline
  dispatch, `simplicio_loop/work_item_claims.py::AttemptCoordinator`), issue #183/#275 (earlier
  distributed-coordination attempts).

## Context

`simplicio_loop/remote_queue.py` already implements a transactional lease/fencing queue
(`simplicio.queue/v1`) with two interchangeable backends behind one `RemoteQueue` protocol:

- `SQLiteRemoteQueue` — a single-host, shared-file backend. Issue #288 wired this into
  `runner.py`'s real dispatch path (`AttemptCoordinator.run_guarded`) so a *co-located*
  coordinator+worker gets heartbeat-guarded, fenced mutation with real receipt verification
  and PR merge.
- `HTTPRemoteQueue` — a real network client (TLS-required off loopback, short-lived bearer
  credentials, connect-time trust enforcement per #289) talking to
  `scripts/remote_queue_server.py`'s stdlib HTTP facade.

Before this change, `simplicio_loop/runner.py::_operator_dispatch_attempt` treated both
backends identically: claim (bare or via `AttemptCoordinator`), then call
`execute_operator()` **in the coordinator's own process**, regardless of which `RemoteQueue`
implementation was in play. That is exactly the gap issue #286 named: *"o proprio coordenador
chama execute_operator()"* even when a real, networked `HTTPRemoteQueue` was configured — so a
"remote" task never actually left the coordinator's process, no matter how many independent
`RemoteWorkerDaemon` processes existed elsewhere.

## Decision

**The queue *implementation type* is the dispatch-mode switch, not a separate config flag.**

- `SQLiteRemoteQueue` continues to mean "same host, coordinator is also the executor" — the
  #288 guarded-dispatch path (claim → `run_guarded` → complete, all in-process) is unchanged,
  and every existing #288 test keeps passing untouched.
- `HTTPRemoteQueue` now means "genuine remote worker" by default
  (`_remote_worker_dispatch_enabled()`, opt-out only via `SIMPLICIO_REMOTE_WORKER_ONLY=0` for
  a deliberate same-host smoke test). `_operator_dispatch_attempt` short-circuits to
  `_operator_dispatch_attempt_remote_worker`: it **enqueues** the task envelope (below) and
  **polls** `queue.task()` for a terminal `completed` status, bounded by
  `SIMPLICIO_REMOTE_DISPATCH_TIMEOUT_SECONDS` (default 3600s). It never calls
  `execute_operator()` and never claims the task itself — claiming is exclusively the job of
  an independent `RemoteWorkerDaemon` process (`simplicio_loop/worker_daemon.py`,
  `scripts/remote_worker_daemon.py`) running elsewhere, reachable only over the wire.
- A timeout is reported as `reason_code: "remote_worker_timeout"` — a specific, honest
  failure, never a silent fallback to local execution.

## Envelope: `simplicio.remote-worker/v2`

`contracts/remote-worker/v2/schema.json` defines the payload the coordinator enqueues
(`RemoteQueue.enqueue(task_id, payload)`) — `task_id`, `run_id`, `idempotency_key`, `goal`,
`acceptance_criteria`, `dependencies.depends_on`, `source.base_sha`/`fetch_strategy`,
`allowed_paths`, `capabilities_required`, `limits`, and a `context_digest` (`sha256:<hex>` over
the execution-relevant fields) a worker validates before creating a workspace. It deliberately
does **not** duplicate `attempt_id`/`lease_id`/`fencing_token`/`agent_id` — those remain owned
by `simplicio.queue/v1`'s lease record, so there is exactly one source of truth for each
concern. It also structurally forbids `token`/`secret`/`credentials`/`env`/`transcript` keys
(a `not/anyOf` schema clause), matching the issue's "nenhum token, variável de ambiente,
transcript ou segredo" requirement in the context pack.

## State machine (unchanged from the issue, formalized here)

```
queued -> claimed -> running -> {succeeded | failed_retryable | failed_terminal | cancel_requested}
cancel_requested -> {cancelled | succeeded}   (compare-and-swap only; see remote_queue.py's
                                                fencing-token owned-row check, `_owned()`)
```

`failed_retryable` / expired lease / explicit release -> `queued` with a new `attempt_id` and a
strictly larger fencing token (`SQLiteRemoteQueue.claim`'s `token = current.fencing_token + 1`).
`succeeded`, `failed_terminal`, `cancelled` are terminal. This ADR does not change the state
machine implementation (`remote_queue.py`, `worker_daemon.py` already enforce it); it documents
the machine as the contract the envelope and dispatch split above are built against.

## Consequences

- `runner.py`'s coordinator loop is now honest about "remote": configuring
  `SIMPLICIO_REMOTE_QUEUE_URL` genuinely requires a separate `RemoteWorkerDaemon` process to
  make progress — a lone coordinator process against an HTTP queue will enqueue and then time
  out, which is the correct, fail-closed behavior rather than a quiet in-process shortcut.
- A cross-device deployment without a receipt-fetch endpoint cannot yet read the remote
  worker's evidence receipt from the coordinator's filesystem; `_verify_worker_receipt_pair`
  therefore reports `UNVERIFIED` in that case rather than fabricating `VERIFIED`. Closing that
  gap (a receipt-fetch API) is tracked as a remaining item, not silently assumed done.
- Operational worker-daemon supervision (start/monitor/restart across a crash) is a separate
  concern from this dispatch-mode split; see `scripts/remote_worker_supervisor.py` and
  `tests/test_remote_worker_supervisor.py`.
