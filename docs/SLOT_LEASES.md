# Slot leases and fencing

`LeaseStore` is the durable authority for one resource key. SQLite transactions
serialize acquire, heartbeat, release, reclaim and protected writes across
processes on Windows, macOS and Linux.

## Lifecycle

1. `acquire` creates a new attempt and monotonically increasing fence.
2. `heartbeat` renews only the current unexpired owner/fence.
3. `mark_stale` records expiration before reassignment.
4. `reclaim` creates a new attempt/fence; the previous owner can no longer call
   `heartbeat`, `release` or `put`.
5. `put` validates and writes under the same transaction. Receipts always bind
   resource, owner, attempt and fence.

TTL must be positive and cannot exceed `max_ttl_seconds` (one hour by default),
so a skewed caller cannot create infinite validity. Expiry uses persisted wall
time to survive process restarts. Operational deployments should synchronize
host clocks; rollback is switching callers back to the previous coordinator
while retaining the SQLite journal for audit.

## Evidence

Run:

```bash
python -m pytest -q tests/test_slot_lease.py
```

The suite covers a six-process acquire race, persisted holder death/recovery,
stale heartbeat and writer rejection, bounded clock skew, release fencing,
deterministic receipt hashing and a 250-step property reducer.

No LLM/provider is started by this component. Runtime performance metrics are
`null`: this change proves safety behavior and does not claim benchmark gains.
