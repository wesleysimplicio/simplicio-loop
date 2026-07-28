# Typed recovery

Issue [#812](https://github.com/wesleysimplicio/simplicio-loop/issues/812)
adds a deterministic recovery boundary for work whose effects may outlive a
failed transport.

`TypedRecoveryController` classifies failures as `transient`, `permanent`,
`policy`, `semantic`, or `effect_unknown`. Known technical failures never
invoke an LLM:

- transient failures retry inside both attempt and elapsed-time budgets, with
  bounded exponential backoff and deterministic jitter;
- permanent and policy failures stop after one attempt;
- semantic failures return `replan_required`; a caller may then choose a
  reasoning provider explicitly;
- `effect_unknown` never repeats the operation. It reconciles the idempotency
  key and succeeds only with a committed effect receipt; otherwise it blocks;
- cancellation propagates after writing a terminal receipt, allowing the
  operation's `finally`/context-manager cleanup to release resources.

Every attempt receives a causal parent and a strictly newer fence. The
hash-chained, fsync'd JSONL journal survives restart, supplies the next fence,
and materializes successful idempotency receipts so a completed effect is not
executed twice.

## Verification

```console
python -m pytest -q tests/test_typed_recovery.py
python -m pytest -q tests/test_typed_recovery.py tests/test_recovery_unit.py \
  tests/test_async_io_supervisor.py tests/test_async_io_supervisor_system.py \
  tests/test_hub_queue_retry.py tests/test_worker_daemon_unit.py \
  tests/test_model_router_fallback_unit.py
```

The first command covers taxonomy, retry budget, reconciliation, cancel during
write and crash/restart. The second proves compatibility with existing cursor,
process, queue, worker and model-router recovery paths.

Rollback is additive: stop constructing `TypedRecoveryController`; existing
recovery APIs are unchanged. Residual risk is storage-level concurrency: this
slice assumes one writer per journal path. Cross-process journal locking should
be added before multiple controllers share the same path.
