# GitHub issue lifecycle — one canonical status comment (#285)

Issue #285 asks for a full, typed `SourceAdapter` for GitHub — `list_ready` / `get_details` /
`claim` / `update_status` / `attach_evidence` / `close` / `requery` / `reconcile`, an outbox,
lease/fencing-gated ownership, duplicate-comment recovery, and a live sandbox E2E. That is a
multi-week surface. This document describes the real, tested slice landed so far — not the
aspirational full spec — and calls out explicitly what is still missing.

## What already existed (issue #295, merged before this work)

`scripts/pr_evidence.py::publish_comment` / `find_existing_comment` already provide a fail-closed,
idempotent, marker-based create-or-update primitive for a GitHub issue comment:

* discovers a prior comment by a hidden marker (paginated `gh api .../issues/{n}/comments`);
* creates once, or PATCHes the SAME comment id on every subsequent call — never appends a second
  comment for the same marker;
* sends the body as a JSON payload on stdin (`gh api ... --input -`), never via shell
  interpolation of untrusted issue/AC text;
* raises `PublishError` (never a silent "success") on any `gh` failure — auth, network, a
  non-existent issue, etc.

## What #285 adds on top of that (this change)

`simplicio_loop/github_lifecycle.py` (CLI: `scripts/github_lifecycle.py`) adds, reusing the #295
primitive rather than duplicating it:

1. **A validated lifecycle state machine** — `LIFECYCLE_STATES` (`DISCOVERED` → `CLAIMED` →
   `PLANNED` → `IN_PROGRESS` → `VERIFYING` → `PR_OPEN` → `MERGE_READY` → `MERGED` → `CLOSING` →
   `CLOSED` → `RELEASED`, plus the side states `BLOCKED` / `PAUSED_NETWORK` /
   `AWAITING_DECISION` / `CLOSE_PENDING_RECONCILIATION`). `validate_transition(from, to,
   reason_code=...)` rejects any hop that isn't a valid forward transition unless an authorized
   regression `reason_code` is given (`SOURCE_CHANGED`, `CHECKS_REGRESSED`, `REVIEW_REOPENED`,
   `LEASE_REASSIGNED`, `DELIVERY_REGRESSED`) — a duplicate event (same state twice) is always a
   no-op.
2. **A deterministic, sanitized renderer** — `render_lifecycle_comment(...)` renders the exact
   table + sections shape the issue describes (identity/lease/fence/branch header table,
   Objetivo e escopo, Critérios de aceite checklist, Plano passo a passo, Progresso e blockers,
   Testes e evidências, Entrega), using its OWN hidden marker
   (`<!-- simplicio-loop:lifecycle-status:v1 -->`) so it never collides with the #295
   evidence-comment marker on the same issue. Every field is redacted (token/secret patterns)
   before rendering.
3. **Publish-then-re-query confirmation** — `publish_lifecycle_state(...)` publishes via the #295
   primitive, then immediately re-fetches the same comment and compares the observed body hash
   against the expected one, producing a `simplicio.github-lifecycle-receipt/v1` receipt with
   `verified: bool`. A write that "succeeds" per `gh`'s exit code but whose re-query observes a
   different body (e.g. a race) is reported `verified: False` / `outcome: "blocked"`, never
   silently accepted as done.
4. **Deterministic operation ids** — `operation_id(provider, repo, issue, run_id, attempt_id,
   fencing_token, lifecycle_revision, operation_kind)` for future idempotency-key/outbox wiring.

### Example

```python
from scripts.pr_evidence import publish_comment
from simplicio_loop.github_lifecycle import publish_lifecycle_state

receipt = publish_lifecycle_state(
    owner="acme", repo="widgets", issue="12", state="PLANNED",
    run_id="run-1", attempt_id="issue-12-1", fencing_token="7",
    publish_comment_fn=publish_comment,
    goal="Add SSO login", acceptance_criteria=[{"id": "AC-1", "text": "renders the button"}],
)
# receipt["comment_id"] is the SAME id across CLAIMED -> PLANNED -> ... -> CLOSED
# receipt["verified"] is True only if the re-queried body hash matches what was just sent
```

CLI: `python3 scripts/github_lifecycle.py publish --owner acme --repo widgets --issue 12
--state PLANNED --run-id run-1 --attempt-id issue-12-1`.

## Tests

`tests/test_github_lifecycle_unit.py` (18 tests, no real `gh`/network call — a fake `runner`
callable services the fake transport, same style as `scripts/pr_evidence.py`'s own selftest):
state-machine coverage (happy path, duplicate no-op, invalid jump, authorized regression),
renderer coverage (marker present, AC checklist rendering, secret redaction, determinism,
unknown-state rejection), and the publish/re-query path (first claim creates one comment, every
subsequent state update reuses the SAME comment id across the full CLAIMED→CLOSED path, and a
simulated race where the re-query observes a different body is reported unverified/blocked, never
a fake pass).

## Explicitly NOT implemented here (tracked, not claimed done)

* `list_ready` / `get_details` / `requery` / `reconcile` — the read-side verbs of the full
  `SourceAdapter` protocol.
* Lease/fencing-gated comment ownership (`CLAIM_CONFLICT` when another active lease owns the
  comment) — today `publish_lifecycle_state` will happily update whatever comment the marker
  finds, with no lease check of its own (the runner-level lease/fencing from
  `simplicio_loop/work_item_claims.py` is a separate, already-existing mechanism this module does
  not yet call into).
* An outbox for crash recovery between the remote write and a persisted local receipt.
* Duplicate-comment detection/repair across two authors/devices.
* The close-transaction two-re-query flow (`CLOSING` → re-query issue state=`closed` → update
  comment to `CLOSED` → re-query again) — the state machine models the states, but nothing here
  yet calls `gh issue close` or the second re-query.
* A live, opt-in sandbox-repo E2E test.
* Wiring `scripts/pr_evidence.py comment --publish` (or an equivalent runner event) to actually
  call `publish_lifecycle_state` for CLAIMED/PLANNED/etc. — this PR adds the primitive; wiring it
  into the runner's real event stream (`_emit_event` in `simplicio_loop/runner.py`) is follow-up
  work.
