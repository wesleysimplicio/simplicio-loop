# Delivery target receipts

A completion receipt is not only a boolean promise result. When a run directory is available, `completion-receipt.json` records both `delivery_target` and the observed `delivery_state`. The oracle may return `COMPLETE` only when the state satisfies the frozen target and all other gates pass.

Supported targets/states are ordered: `implemented`, `verified`, `pr-open`, `merge-ready`, `merged`, `released`, and `deployed`. A receipt with `ready: false` remains `DELIVERY_PENDING` (or another typed blocker) and must not be used to close the source issue.

Example:

```json
{
  "verdict": "COMPLETE",
  "delivery_target": "verified",
  "delivery_state": "verified",
  "tag": "MEASURED"
}
```

Consumers should display both fields in status and handoff output. A later source requery can move the state backwards (for example, `merge-ready` to `pr-open`) and must reopen the loop with a reason code rather than silently preserving completion.

## Local requery/race contract

`reconcile_delivery_observation(previous, current)` emits a
`simplicio.delivery-reconciliation/v1` record. It compares canonical
`source_fingerprint` values and classifies the transition as `advanced`,
`unchanged`, `observed`, `reopened`, or `stale`:

- `reopened` is emitted when a previously ready target fails a fresh gate (for
  example CI turns red between two queries); the reason code is copied from the
  failed gate and the runner returns to `partial`/`requery_source`.
- `stale` is emitted when an optimistic writer's expected previous fingerprint
  no longer matches, preventing a concurrent observation from being silently
  overwritten.
- equal fingerprints are idempotent and classify as `unchanged`.

This is a deterministic local receipt contract. It does not claim that GitHub,
release registries, signatures, or credentials are available; adapters must still
perform the real external requery and supply its payload.
