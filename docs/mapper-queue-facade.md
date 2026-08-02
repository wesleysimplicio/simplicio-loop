# MapperStore queue facade

`simplicio_loop.mapper_queue.MapperQueue` is the explicit queue-facing facade
for `operations.sqlite`. It delegates enqueue, atomic claim, heartbeat, release,
completion receipts, cancellation, reclaim, checkpoint, and status to
`MapperOperationsAdapter`.

The facade has no `sqlite3` import, no local schema, and no fallback to
`LocalTaskQueue`. Construction is side-effect free; `initialize()` is the only
explicit initialization call. A completion without a receipt and an enqueue
without an idempotency key fail before reaching a store.

This slice does not yet switch every existing Loop runner/CLI call site to the
facade. The legacy queue remains available only for the compatibility window;
cutover, import/rollback, standalone/runtime-backed E2E, and final DDL removal
remain tracked by #1026/#1027.
