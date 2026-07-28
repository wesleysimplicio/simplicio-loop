# Durable RunJournal

`RunJournal` stores a causal, hash-chained event log in SQLite WAL. Every append
uses `BEGIN IMMEDIATE`, a per-run sequence and a unique idempotency key. Replay
is a pure Python reducer and never invokes an LLM, provider or network.

Effects use two durable boundaries:

1. `checkpoint_before_effect` persists intent.
2. Execute the external effect with its own idempotency key.
3. `checkpoint_after_effect` persists the remote receipt.

After a crash, `replay()` exposes pending effects and confirmed effects. A
terminal receipt seals the run; later events fail with
`terminal_receipt_exists`. Out-of-order sequence and causal parents return
stable reason codes.

Integrity is fail-closed: SQLite integrity checks gate writes, while replay
verifies sequence, causal parent and every SHA-256 chain link. Migrations run
inside a transaction and roll back both schema and version on failure.
`backup()` uses SQLite's online backup API and validates the result.
`restore()` validates both source and restored database. Verified snapshots can
compact live events into the immutable archive without changing replay output.

Run the evidence:

```bash
python -m pytest -q tests/test_run_journal.py
```

The suite includes process concurrency, crash/fault boundaries, duplicates,
out-of-order events, terminal sealing, migration rollback, tampering,
corruption, compaction and backup/restore. Performance metrics are `null`
because no performance benchmark is claimed.
