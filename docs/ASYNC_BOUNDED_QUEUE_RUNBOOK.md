# AsyncBoundedQueue operational runbook (issue #508)

This is the operational companion to `simplicio_loop/async_bounded_queue.py`. It
covers what actually failing/degrading looks like given the *current* code, how an
operator would notice, and how to disable/roll it back. It is intentionally short
because the component's real-world footprint is currently narrow: see the
"Deployment status" note below before reading this as a production incident guide.

## Deployment status (read this first)

As of this writing, `AsyncBoundedQueue` has **no caller outside its own test suite and
`scripts/benchmark_async_queue.py`** (`grep -rn "AsyncBoundedQueue(" simplicio_loop/
scripts/` finds only the two constructions inside the benchmark script). It is not
wired into any ingestion, dispatch, or report path. There is no feature flag guarding
it because there is nothing in production consuming it to flag off. That is a real,
open gap tracked in issue #508's own comment history — the primitive is well tested in
isolation, but "aplicar" (apply it to ingestão/dispatch/reports) has not happened yet.

Consequences for this runbook:
- The failure modes below are about the primitive's *contract*, for whichever future
  call site wires it in, not about a live incident path today.
- "Rollback" for the module as it exists today is trivial: nothing imports it in a
  request path, so removing/reverting the file (or reverting the commit that added it)
  has zero blast radius on running services. The interesting rollback question — how a
  future caller unwires it under load — is documented in "Rollback for a future caller"
  below so whoever does the wiring doesn't have to re-derive it.

## Failure modes in the current code

1. **`BackpressureError` on `put()`** — raised when `overload="reject"` and the queue
   is at `max_items`/`max_bytes`, or when `overload="wait"` with a `timeout` that
   expires while the caller is queued (`async_bounded_queue.py` lines 121-136). The
   exception carries a `receipt` dict (`reason`: `"full"` or `"timeout"`,
   `queued_items`, `queued_bytes`, `wait_ms`) — log/re-raise that receipt rather than a
   bare exception string; it is the only structured signal for *why* a producer was
   shed.
2. **Unbounded wait in `overload="wait"` mode with no `timeout`** — a producer against
   a queue whose consumers have stalled (e.g. a consumer task crashed without draining)
   blocks on `self._condition.wait()` forever (line 127). This is not a bug in the
   queue — it is the documented backpressure contract — but a caller that always omits
   `timeout` has no way to detect this except an external watchdog on the producer
   coroutine's wall-clock time.
3. **`task_done()` called more times than items were dequeued** — raises `ValueError:
   task_done called too many times` (line 162). This indicates a caller bug (double
   ack) and will surface immediately as an unhandled exception in whichever consumer
   loop mis-tracked its acks.
4. **`reopen()` called while items or unfinished acks are pending** — raises
   `RuntimeError: cannot reopen while work is pending` (line 190). A caller that
   restarts a queue without first draining will see this rather than silently losing
   items.
5. **Coalescing (`coalesce=True`) silently drops the value on a repeated key** — this
   is by design (last-write-wins on that key), but a caller expecting per-key delivery
   guarantees rather than "latest wins" would misread the `status()["coalesced"]`
   counter as harmless when it represents work that was actually discarded.

## Detecting degradation

`status()` returns a dict with `accepted`, `coalesced`, `rejected`, `wait_count`,
`items`, `bytes`, `unfinished`, and `closed`. There is no push metric today — a caller
must poll `queue.status()` explicitly and export it (there is no `/metrics` endpoint or
log line emitted internally by the module itself). Signals worth alerting on once a
call site exists:
- `rejected` climbing while `overload="reject"` — producers are being shed; check
  whether `max_items`/`max_bytes` are sized correctly for the workload or whether
  consumers have stalled.
- `wait_count` growing much faster than `accepted` — producers are queueing behind a
  slow/stuck consumer; this is `overload="wait"`'s expected behavior under load, but a
  sustained climb with flat `accepted` means consumers stopped draining.
- `items` pinned at `max_items` with `unfinished` not decreasing — a consumer is
  stuck between `get()` and `task_done()` (e.g. an unhandled exception inside the
  consumer body that skips the `task_done()` call).

`scripts/benchmark_async_queue.py` is the only script that exercises the queue under
load today; it is a benchmark, not a monitor, and does not run continuously.

## Rollback for a future caller

There is no environment variable or CLI flag today because there is no wired-in call
site to gate. When a caller does wire `AsyncBoundedQueue` into an ingestion/dispatch/
report path, the two rollback levers available from the constructor itself, with no
code change beyond a config value, are:

- `overload="reject"` → `overload="wait"` (or vice versa) — swap between "shed load
  immediately" and "apply backpressure upstream" without touching queue internals.
- Raise `max_items`/`max_bytes` as an emergency valve, or set `timeout=None` on `put()`
  calls to stop time-boxed rejections, while a real capacity fix is rolled out.

For a full revert, since the module has zero production callers as of this writing,
`git revert` on the commit(s) that introduce the call site is sufficient — there is no
in-flight state (persisted queue, external process) to migrate away from, because the
queue is in-process and in-memory only (confirmed by `reopen()`'s guard against
reopening with pending work, and by the absence of any file/socket/db handle anywhere
in `async_bounded_queue.py`).

## Test and coverage evidence

`python3 -m coverage run -m pytest -q tests/test_async_bounded_queue.py
tests/test_async_bounded_queue_system.py tests/test_async_bounded_queue_integration.py
tests/test_async_bounded_queue_restart.py` followed by `python3 -m coverage report -m
--include='simplicio_loop/async_bounded_queue*'` measured **100% line coverage**
(129/129 statements) on `simplicio_loop/async_bounded_queue.py` on 2026-07-18, with all
26 tests across the four files passing.
