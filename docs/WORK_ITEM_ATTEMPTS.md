# Bounded WorkItem attempts

`simplicio_loop.work_item_claims.AttemptCoordinator` is the local bridge between the
state-machine runner and the Runtime queue lease:

1. `claim()` atomically obtains one fenced lease and builds an allow-listed context pack.
2. `record_event()` and `accept_receipt()` first call `assert_active()`, so a stale worker
   cannot append accepted tool/evidence output after expiry or reassignment.
3. `retry()` releases the current lease and claims a new fencing token/attempt.
4. `complete()` persists the queue completion only after the current lease is checked.

SQLite and the HTTP queue facade implement the same read-only `assert-active` operation. A
queue outage raises `QueueUnavailable`; callers must hand off rather than mutate. This is a
measured local contract, not proof that an external board or multi-device service is deployed.

```python
from simplicio_loop.remote_queue import SQLiteRemoteQueue
from simplicio_loop.work_item_claims import AttemptCoordinator

attempt = AttemptCoordinator(queue, run_id="run-1").claim(
    work_item_id="WI-1", identity=identity, goal="...", acs=["AC-1"],
    allowed_paths=["src/feature.py"],
)
coordinator.accept_receipt(attempt, {"status": "passed"})
coordinator.complete(attempt, receipt_ref="receipts/WI-1.json")
```

Focused proof lives in `tests/test_work_item_claims.py` and the HTTP assertion is covered by
`tests/test_remote_queue.py`.
