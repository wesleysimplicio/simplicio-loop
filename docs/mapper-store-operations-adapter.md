# Loop MapperStore operations adapter

`simplicio_loop.mapper_operations.MapperOperationsAdapter` is the first
operations slice of Loop issue #1026. It delegates queue state, slots, leases,
fencing, receipts, checkpoints, effect intents, and the journal to the
installed `simplicio_mapper.store.OperationsStore` API.

The adapter does not open SQLite or inspect Mapper tables. Constructing it is
side-effect free; callers must call `initialize()` explicitly before the first
write. If the Mapper API is missing or an operation fails, the adapter raises
and never falls back to a Loop-owned database.

`claim_next()` intentionally exposes Mapper's atomic claim-by-worker operation.
It is not a DAG planner and does not replace the remaining Loop queue/lease
call sites. Those call sites, differential tests, import idempotency, and the
final cutover remain acceptance work for #1026/#1027.
