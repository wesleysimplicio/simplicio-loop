# Async I/O migration runbook (#509)

Scope: the two pieces of the #509 async migration that ship real, wired code today —
`AsyncProcessSupervisor` (`simplicio_loop/async_io_supervisor.py`) and the `*_async` methods
added to `HTTPRemoteQueue` (`simplicio_loop/remote_queue.py`: `enqueue_async`, `claim_async`,
`heartbeat_async`, `complete_async`, `pull_async`, and the shared `_request_async` helper). Per
issue #509's own comment history, neither has a CLI/system caller yet — this doc covers the
component as it exists in the tree, not a hypothetical wired deployment.

## What can actually go wrong (from the current code)

1. **State file read failure is silent.** `AsyncProcessSupervisor._load_state` (lines 58-64)
   catches `OSError`/`ValueError` on a corrupt or unreadable `state_path` and just returns —
   `_outcomes` stays empty and `_recovered_leases` stays empty. A supervisor restarted with a
   damaged state file will not report *any* recovered leases and will not error; it silently
   forgets in-flight idempotency outcomes instead of failing loudly.
2. **Every lease from a previous process is always "recovered", never resumed.** `_load_state`
   (lines 74-79) treats every on-disk lease as abandoned on the restart boundary regardless of
   whether its wall-clock TTL has actually elapsed. This is deliberate (no in-process owner
   survives a restart), but it means a lease that was one second from completing before a crash
   is unconditionally surfaced via `status()["recovered_leases"]`, not silently re-run.
3. **`DuplicateLease` / `SupervisorClosed` are raised, not swallowed.** Calling `run()` again
   with a `lease_id` still in `self._tasks` raises `DuplicateLease`; calling `run()` after
   `shutdown()` has set `self._draining = True` raises `SupervisorClosed`. A caller that does not
   catch these will crash outright — there is no silent retry inside the supervisor.
4. **`_request_async` cannot cancel the underlying network thread.** The docstring on
   `HTTPRemoteQueue._request_async` (`remote_queue.py` lines 218-229) states the real limitation:
   the blocking `urlopen` call runs via `loop.run_in_executor`, and `urlopen` has no cooperative
   cancellation hook. When `asyncio.wait_for`'s deadline or a task cancellation fires first, the
   awaiting coroutine gets `QueueUnavailable`/`CancelledError` back immediately, but the executor
   thread underneath keeps blocking on the socket until *its own* timeout (`self.timeout`, the
   `HTTPRemoteQueue` constructor argument, independent of the async `timeout=` argument) elapses.
   A caller that repeatedly issues short-`timeout` async calls against a slow/unresponsive server
   without also lowering `self.timeout` can pile up executor threads waiting out the longer
   socket timeout — this is a real thread-pool pressure risk, not a hypothetical one, and it is
   documented in-code, not fixed, because `urlopen` gives no way to fix it from the caller side.

## How an operator detects degradation

- **Supervisor health:** call `AsyncProcessSupervisor.status()`. Watch `active_leases` /
  `active_tasks` growing without bound (leases never clearing means `run()` calls are stuck or
  its `finally` block isn't reached), `semaphore_available` pinned at `0` (all `max_concurrency`
  slots are permanently in use), and `draining: true` while callers still expect new work to be
  accepted (shutdown was called but callers didn't stop submitting).
- **Restart correctness:** after a restart with `state_path` set, inspect `recovered_leases` in
  the first `status()` call — a non-empty list after a crash is expected and informational; an
  empty list after a crash where leases were known to be active means the state file was
  unreadable (failure mode 1 above) and should be treated as a loss of idempotency-outcome
  memory, not a clean restart.
- **Remote-queue thread exhaustion:** there is no exposed counter for in-flight `run_in_executor`
  futures. The observable symptom is `*_async` call latency creeping up toward — or getting
  stuck at — `self.timeout` (the `HTTPRemoteQueue(..., timeout=...)` constructor value) even for
  requests that should be fast, because the default executor's thread pool is occupied by
  earlier calls still waiting out their own socket timeout. If this is suspected, check process
  thread count (e.g. `ps -T -p <pid> | wc -l` on Linux) against the expected baseline.

## Rollback

Neither component has a production call site wired into a CLI or daemon today (confirmed by
`grep` across the tree per the #509 comment thread), so rollback is a code-level revert, not a
flag flip:

- **`AsyncProcessSupervisor`**: stop constructing/calling it; use the existing synchronous
  `PythonProcessAdapter`/`ProcessSupervisor` path in `simplicio_loop/process_supervisor.py`
  directly. There is no env var or CLI flag to disable it because nothing currently routes
  through it outside its own tests.
- **`HTTPRemoteQueue.*_async` methods**: stop calling the `*_async` variants and use the
  original synchronous methods (`enqueue`, `claim`, `heartbeat`, `complete`, `pull`) on the same
  `HTTPRemoteQueue` instance — they are unchanged and remain the default code path. Each `*_async`
  method is additive; deleting the call site that invokes it is the entire rollback.
- If a future caller wires either component in and needs a runtime kill-switch, that switch does
  not exist yet in this codebase — add one (e.g. an env var gating which adapter/method a caller
  picks) at the point of integration; this doc will need updating once such wiring lands.
