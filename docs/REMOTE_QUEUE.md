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

## HTTP network adapter

The repository now ships a stdlib-only HTTP facade and client. The server keeps
the SQLite transaction boundary authoritative while clients on separate
machines use the same claim/lease/fencing protocol:

```powershell
python scripts/remote_queue_server.py --db .orchestrator/shared-queue.db --host 0.0.0.0 --port 8765 --token "$env:SIMPLICIO_QUEUE_TOKEN"
```

```python
from simplicio_loop.remote_queue import HTTPRemoteQueue
q = HTTPRemoteQueue("https://queue.example.internal", token=os.environ["SIMPLICIO_QUEUE_TOKEN"])
lease = q.claim("issue-185", "claude@machine-b", idempotency_key="run:185")
q.complete(lease, receipt_ref="receipts/185.json")
```

Authentication is required when the server is configured with `--token`; every
transport failure raises `QueueUnavailable` and therefore pauses mutation. A
stale lease remains rejected by the server's SQLite fencing check. Do not use
GitHub issue labels as a lock. For production, put the service behind TLS and a
network policy; Redis/SQL service implementations can target the same protocol.

### Measured local multi-worker proof

The live transport test starts the HTTP facade and then uses two independent
spawned Python processes (one Codex-style identity and one Claude-style
identity). It verifies bearer-token rejection, one atomic winner, expiry
reclaim with a higher fencing token, stale completion rejection, monotonic
event replay, and the completion `receipt_ref`:

```powershell
python -m pytest -q tests/test_remote_queue_live.py tests/test_remote_queue.py
```

This is a local process-isolation proof of the wire contract. It does **not**
claim TLS termination, firewall policy, or a live deployment on separate
physical machines; those remain deployment acceptance gates.
