# Maintenance-deferred runnable example

This example demonstrates the exact acceptance path for issue #144 without mutating production code:

1. an active run starts for a broader goal;
2. a maintenance-owned correction is captured as backlog-only;
3. the run resumes and points back to the normal broader-goal flow.

Run it from the repository root:

```powershell
python examples/maintenance-deferred/run_example.py
```

What it proves:

- `maintenance-receipt.json` is created under the active run;
- the correction is preserved with an explicit deferral reason and resume steps;
- `completion.ready` stays `false` while the run is backlog-only;
- the original operator receipt is not mutated during deferral capture;
- `resume` switches the run back to active maintenance mode and forces `next_action=mapper_scan_required`.

The script is deterministic because it seeds a minimal active-run fixture on disk and then exercises the real maintenance-deferral and resume functions against that fixture.
