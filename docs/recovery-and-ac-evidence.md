# AC evidence and crash-safe recovery

The loop and Runtime exchange two transport-neutral JSON contracts:

* `simplicio.ac-evidence-receipt/v1` binds every acceptance criterion to a
  reproducible command, zero exit code, artifact SHA-256, provenance, claim type,
  actor and environment identity. `measured`, `replayed`, and `benchmarked`
  claims are accepted; an `estimated`-only criterion fails closed.
* `simplicio.loop-cursor/v1` stores the run, WorkItem, Attempt, actor and
  environment identity together with the last acknowledged sequence, exact event
  IDs, projection hash and terminal marker.

`reconcile_after_crash(events, cursor)` is idempotent. It accepts an already
acknowledged event only when its complete envelope and event ID match the cursor
history. It blocks on altered duplicates, sequence gaps, identity drift, and
unknown acknowledged events. A cursor whose last event is terminal returns
`execution_allowed: false`, so a completed WorkItem is not executed again after a
process or host restart.

Cursor writes use a temporary file, `fsync`, atomic `os.replace`, and a best-effort
directory `fsync`. A Runtime may persist the same cursor in its durable store; the
wire shape and conflict policy remain identical across provider handoff.

```python
from simplicio_loop.recovery import reconcile_after_crash

cursor, diagnostics = reconcile_after_crash(runtime_events, persisted_cursor)
if not diagnostics["execution_allowed"]:
    return  # terminal WorkItem: acknowledge only, never re-execute
```

The tests in `tests/test_recovery.py` cover replay idempotence, terminal suppression,
gap/identity/tamper rejection, atomic persistence, AC coverage, receipt hash
validation, and estimated/non-zero evidence rejection.
