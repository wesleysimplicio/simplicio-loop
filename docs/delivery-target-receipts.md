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

## Commit binding between #283 (quality gate) and #288/delivery evidence

`merge-ready` (and every state above it) is derived from two receipts that are validated
independently: the `#278`/`#283` quality-matrix receipt (`quality-matrix.json`,
`evaluate_quality_matrix`) and the delivery/`#288` evidence (`delivery-receipt.json`,
`validate_delivery_receipt`). Validating each in isolation is not enough — a stale, green
quality-matrix receipt from an earlier commit could sit next to a fresh, green delivery receipt
for the *current* commit, and the oracle would see two passes without either one actually
covering the same code.

`simplicio_loop/oracle.py::_commit_binding_gate` closes this gap: once the delivery receipt's
`current_state` reaches `merge-ready` or higher, completion additionally requires the
quality-matrix receipt to declare `work_item.head_sha` (populated via
`scripts/quality_matrix.py build|populate --work-item-head-sha <sha>`) equal to the delivery
receipt's `pr.head_sha` (or `merge.commit_sha` once merged). Per the "Unknown is not pass"
invariant, an absent `work_item.head_sha` on the quality-matrix side blocks with
`quality_matrix_commit_unbound` rather than defaulting to a pass; a real mismatch blocks with
`quality_matrix_commit_mismatch`. States below `merge-ready` (`implemented`, `verified`,
local-fixture work) are unaffected — this gate only activates once external convergence is
actually being claimed. If a `merge-queue-receipt.json` (`#288`) is present in the run directory,
its `head_sha`/`commit_sha` is cross-checked the same way (`merge_queue_commit_mismatch`).

## Scope note: `deployed` means "installable from verified bytes," not live rollout

Issue #290's semantics table describes `deployed` in terms that read naturally for a
long-running *service* — "o artifact verificado está ativo no environment solicitado," health
checks against a live endpoint, canary/region rollout state, and rollback detection against a
running deployment. `simplicio-loop` ships a Python/npm **package**, not a service with its own
process, endpoint, or environment to curl. There is no server to health-check and no canary
region to poll.

`DeploymentVerifier` (`simplicio_loop/external_verifiers.py`) therefore implements the closest
honest analog available for a package repo: "deployed" is proven by (a) the release commit being
reachable from the real default branch, (b) the exact published bytes being checksum/digest
verified (composing `ReleaseArtifactVerifier`), and (c) those same downloaded bytes genuinely
installing and importing in a clean, throwaway environment (`run_install_smoke`). "environment"
is modeled as the *install target* the smoke ran against (e.g. `"pypi-index"`, `"local-venv"`, a
CI runner image), not a live hostname with nothing behind it.

Given that scope, the following parts of #290's `deployed` acceptance criteria are **not
applicable** to this repo as currently shipped, and are proposed for reduction rather than left
permanently unverifiable:

- live health/smoke against a running endpoint with a nonce (there is no endpoint);
- canary/partial-rollout-vs-global-rollout distinction across regions (there is no rollout
  topology — a package release either exists on the index or it does not);
- rollback-of-a-live-deployment detection (there is no running deployment to roll back; the
  closest real analog is a release being yanked/deleted from the index, which
  `ReleaseArtifactVerifier`'s reachability/checksum re-query already detects on the next
  observation as a regression from `released`/`deployed` back to an earlier state).

If this project ever ships a component that *is* a long-running service (e.g. a hosted API), the
full canary/region/rollback semantics from #290's original table should be revisited and
implemented for that component specifically — this note narrows scope for the package this repo
ships today, it does not weaken the bar for a future service.
