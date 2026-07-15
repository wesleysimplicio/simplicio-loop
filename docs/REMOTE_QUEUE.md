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

Network-facing binds are fail-closed unless TLS is configured. Provide a certificate and
private key (TLS 1.2 or newer) through flags or environment variables:

```powershell
$env:SIMPLICIO_QUEUE_TLS_CERTFILE = "C:\\etc\\simplicio\\queue.crt"
$env:SIMPLICIO_QUEUE_TLS_KEYFILE = "C:\\etc\\simplicio\\queue.key"
python scripts/remote_queue_server.py --host 0.0.0.0 --port 8765
```

Plain HTTP is supported only on loopback for local tests. Production deployments still require
a firewall/network policy, token rotation, and a trusted certificate chain.

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

## Worker daemon: heartbeat loop + cooperative cancellation (issue #286)

`simplicio_loop.worker_daemon.RemoteWorkerDaemon` is a standalone worker that discovers
(`pull`), claims, and heartbeats a task on its own -- independent of any local
coordinator -- for the life of an arbitrarily long unit of work (not just a bounded
subprocess). A background thread heartbeats every `heartbeat_interval` seconds; the
moment the queue reports the lease cancelled (`RemoteQueue.request_cancel`) or the
heartbeat itself fails (reclaimed lease / unreachable queue), the worker's cooperative
`work_fn` is signalled to stop and the outcome is reported as `"cancelled"` or
`"lease_lost"` respectively -- never silently completed under a stale fence:

```python
from simplicio_loop.remote_queue import SQLiteRemoteQueue
from simplicio_loop.worker_daemon import RemoteWorkerDaemon, sleep_in_slices

queue = SQLiteRemoteQueue(".orchestrator/shared-queue.db")
worker = RemoteWorkerDaemon(queue, agent_id="codex@laptop-a", heartbeat_interval=1.0, lease_ttl=5.0)
lease = worker.try_claim("issue-185", idempotency_key="run:185")

def work(check_cancelled):
    return {"finished": sleep_in_slices(30.0, slice_seconds=0.1, check_cancelled=check_cancelled)}

outcome = worker.run_task(lease, work, receipt_ref=".orchestrator/receipts/185.json")
# outcome.status is one of "completed" | "cancelled" | "lease_lost"
```

A cancellation is issued from any other process against the shared queue and is
scoped to the current fencing token (it does not "stick" to a future reclaim):

```python
queue.request_cancel("issue-185", reason="operator requested stop")
```

`scripts/remote_worker_daemon.py claim --db ... --agent-id ... --task-id ...` and
`scripts/remote_worker_daemon.py cancel --db ... --task-id ...` wrap the same class as a
standalone CLI process, used by the real (not mocked) two-OS-process end-to-end test:

```powershell
python -m pytest -q tests/test_remote_worker_e2e.py tests/test_worker_daemon.py tests/test_remote_queue_cancellation.py
```

`tests/test_remote_worker_e2e.py` spawns two genuine `subprocess.Popen` processes
against one shared SQLite file and proves, without mocking any of it: process A claims
and heartbeats a task; process B's claim of the same task is rejected while A's lease is
alive; process A is `kill()`ed mid-task (a real crash, no graceful release); process B
then successfully claims and completes the same task once A's lease genuinely expires.
A second scenario proves cooperative cancellation across three real processes (claimant,
canceller, and the queue file) without killing anything.

### Trust-policy hardening for network destinations (issue #289)

Setting `SIMPLICIO_REMOTE_ENVIRONMENT_ID` on the `simplicio_loop.runner` side
(see `_distributed_configuration`) turns the queue destination into a
policy-resolved, enumerated `environment_id` instead of a freeform URL
(`.github/security/distributed-trust-policy.json`, `scripts/distributed_trust_policy.py`).
Once that's set:

* `HTTPRemoteQueue` requires every request to pass `check_endpoint()` -- DNS
  resolved once and pinned to the connection to avoid rebinding, disallowed
  addresses (loopback/link-local/private/metadata) rejected before connecting,
  zero HTTP redirects, and the *measured* TLS leaf certificate hashed and
  compared against the policy's pins -- before any bearer token is written to
  the socket (`simplicio_loop.secure_transport`).
* The bearer credential itself can be short-lived instead of a static secret:
  set `SIMPLICIO_REMOTE_QUEUE_TOKEN_SECRET` (an HMAC signing secret, never
  sent on the wire) and the runner mints a fresh token per process via
  `scripts/short_lived_credentials.py`, bound to the worker's agent identity
  and the environment_id, expiring in `max_ttl_seconds` (or
  `SIMPLICIO_REMOTE_QUEUE_TOKEN_TTL_SECONDS` if lower). Run the queue server
  with `--token-secret`/`--token-scope`/`--revocation-store` (or the
  matching `SIMPLICIO_QUEUE_*` env vars) to verify it and support immediate
  revocation by `jti` (`scripts/short_lived_credentials.py revoke`).
* This is not the full OIDC broker exchange #289 describes (no CI identity
  provider exists in this repo to issue the initial trust), and job
  separation / GitHub Environment protection do not apply now that
  `.github/workflows/distributed-183-proof.yml` has been removed (#311) --
  see the issue thread for the current, re-scoped remaining gaps.

### Receipt verification wired into the remote-queue completion path (issue #288)

`work_item_claims.AttemptCoordinator.accept_receipt(..., schema=...)` now optionally runs
the bound receipt through `receipt_verifier.verify_receipt` (schema/hash/freshness/
provenance) before recording it, and `AttemptCoordinator.verify_and_complete(...)` gates
the queue's `complete()` transition on a `VERIFIED` verdict -- a non-`VERIFIED` result
raises `ReceiptVerificationFailed` and leaves the lease active instead of silently
transitioning the task to `completed`. `schema=None` preserves the pre-#286
existence-only behavior for callers that have not adopted a schema yet.
