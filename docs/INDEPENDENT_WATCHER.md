# Independent watcher plan

`scripts/independent_watcher.py` is a fail-closed behavioral verifier for a
committed implementation snapshot. It receives a versioned plan containing a
challenge, run id, commit/diff fingerprints, and one safe command per
acceptance criterion. It then archives the exact commit into a temporary
directory and executes each command from that clean snapshot in a separate
process. It does not consume the implementer's `verification_state` or
`watcher_state.json`.

```powershell
python scripts/independent_watcher.py --repo . `
  --plan .orchestrator/runs/<run>/watcher-plan.json `
  --out .orchestrator/runs/<run>/independent-watcher-receipt.json
```

The plan is rejected when the commit or diff fingerprint is stale, when the
working tree is dirty, when a command is outside the safe executable policy,
or when any criterion exits with an unexpected code. A receipt is
`MEASURED` only when every criterion was recomputed successfully. Dirty trees
must be committed and re-planned; this prevents a watcher from silently
testing `HEAD` while the implementer reports uncommitted behavior.

`contracts/task-to-delivery/golden-corpus.json` is the raw-Markdown corpus
manifest used by the parser regression test. It covers frontend, backend,
full-stack, migration, bug, CLI, docs, security, multi-task DAG, and PLANES
inputs. The corpus test is local contract evidence; published clean-install
and browser evidence remain separate release gates.
