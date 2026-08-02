# Loop MapperStore operations adapter

`simplicio_loop.mapper_operations.MapperOperationsAdapter` is the operations
adapter slice of Loop issues #1026 and #1028. It delegates queue state, slots,
leases, fencing, receipts, checkpoints, effect intents/unknown/reconciliation
transitions, and the journal to the
installed `simplicio_mapper.store.OperationsStore` API.

The adapter does not open SQLite or inspect Mapper tables. Constructing it is
side-effect free; callers must call `initialize()` explicitly before the first
write. If the Mapper API is missing or an operation fails, the adapter raises
and never falls back to a Loop-owned database.

`mark_effect_unknown()` intentionally does not create a receipt; callers must
use `reconcile_effect()` with explicit evidence before retrying or completing.

`claim_next()` intentionally exposes Mapper's atomic claim-by-worker operation.
It is not a DAG planner and does not replace the remaining Loop queue/lease
call sites. Those call sites, differential tests, import idempotency, and the
final cutover remain acceptance work for #1026/#1027.
