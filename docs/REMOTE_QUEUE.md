# Remote queue contract (`simplicio.queue/v1`)

The queue is the shared coordination boundary for Codex, Claude, Cursor, and
other runtimes. A task is mutated only while its lease is valid and its
fencing token is current. A worker that loses connectivity must pause and
produce a handoff; it must not continue mutating a checkout offline.

## Development backend

`simplicio_loop.remote_queue.SQLiteRemoteQueue` is a real transactional backend
for one machine or a shared filesystem that provides SQLite locking:

```python
from simplicio_loop.remote_queue import SQLiteRemoteQueue
q = SQLiteRemoteQueue(".orchestrator/shared-queue.db")
q.enqueue("issue-185", {"source": "github", "number": 185})
lease = q.claim("issue-185", "codex@laptop-a", idempotency_key="run:185")
lease = q.heartbeat(lease)
q.complete(lease, receipt_ref=".orchestrator/receipts/185.json")
```

Claims are idempotent by `idempotency_key`; expired leases are reclaimable and
increment a fencing token. Every transition appends a monotonic event, so a
reconnecting client can call `events(after=last_seq)` and reconcile without
guessing state. The backend never silently falls back when the store is
unavailable (`QueueUnavailable`).

## Network adapters

An HTTP/Redis adapter must implement the same `RemoteQueue` methods with
server-side transactions (`SET NX`/Lua or SQL transaction), durable event
sequence, and identity authentication. Do not use GitHub issue labels as a
lock. Until such a service is configured, this SQLite backend is the honest
single-host mode; it does not claim multi-device coordination over an ordinary
local path.
