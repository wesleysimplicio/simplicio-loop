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
