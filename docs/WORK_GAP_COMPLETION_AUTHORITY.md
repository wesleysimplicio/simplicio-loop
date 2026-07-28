# Work Gap completion authority

The Work Gap Ledger is the mandatory terminal authority for `simplicio-loop`.
Files, tests, open pull requests, merged source, or installed packages are
necessary facts, but none is sufficient alone.

Every acceptance criterion follows:

`UNMAPPED → OWNED → PLANNED → IMPLEMENTED → VERIFIED → INTEGRATED → DELIVERED`

The executor, verifier, and completion auditor must use three different
identities. `DELIVERED` additionally requires:

- implementation, verification, integration, and delivery evidence;
- a terminal source re-query (`merged`, `released`, or `closed`);
- an independently queried installed module whose bytes match the checkout;
- equality between expected, installed, and source commit revisions;
- a valid append-only event chain that replays to the declared snapshot.

Any stale package/source observation, forged event, illegal transition,
dependency cycle, self-approval, missing owner, or missing evidence fails
closed. Regression appends a `REGRESSED` event to the root gap and all
transitive dependents. `WorkGapLedger.explain()` lists the exact remaining
evidence and blockers.

## Operational evidence

`scripts/benchmark_work_gap_ledger_785.py` produces a measured, reproducible
1/20/100/600-item stress receipt, including a raw clean-control ledger. It
requires zero lost items and equal live/replay digests.

`scripts/installed_artifact_e2e_785.py` builds a wheel, installs it in a clean
virtual environment, imports the installed module using an isolated
interpreter, and compares its SHA-256 to the source checkout.

Both paths are deterministic and never invoke an LLM or provider.

## Rollback

Revert the completion-authority commit. Ledger and benchmark JSON files are
append-only evidence and can remain for audit; they grant no authority when
their schema, chain, revisions, or artifact hashes fail validation.
