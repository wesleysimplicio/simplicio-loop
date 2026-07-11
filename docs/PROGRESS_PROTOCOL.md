# Visual progress protocol

Every run can expose a portable `simplicio.progress/v1` event. The event is suitable for an
LLM/chat transcript, a terminal, a dashboard, or another machine consuming JSON. It is derived
from the run's `state.json` and (when present) `completion-receipt.json`; no renderer is allowed
to infer completion from a phase name alone.

```powershell
simplicio-loop progress run-20260710-abc123 --format text --once
simplicio-loop progress run-20260710-abc123 --format json --once
simplicio-loop progress run-20260710-abc123 --format markdown --once
simplicio-loop progress run-20260710-abc123 --format ansi --interval 0.20
```

The text/Markdown output draws the current phase with an icon, progress bar, current/next
action, and three gate indicators (`evidence`, `watcher`, `oracle`). ANSI mode refreshes the
same card while the run is active. JSON mode is stable for adapters and dashboards:

```json
{
  "schema": "simplicio.progress/v1",
  "phase": "validating",
  "percent": 71,
  "status": "RUNNING",
  "gates": {"evidence": false, "watcher": false, "oracle": false}
}
```

`100%` is emitted only when a persisted completion receipt says `ready: true` and its verdict is
`COMPLETE` or `DRAINED`. Missing, stale, or malformed receipts remain `UNVERIFIED`; a terminal
`done` phase without oracle proof is intentionally rendered as `99%`.
