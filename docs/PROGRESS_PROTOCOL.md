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
# terminal sem suporte a Unicode/ANSI (também funciona no PowerShell sem TTY)
simplicio-loop progress run-20260710-abc123 --format text --ascii --no-animation
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

Producers that have measured sub-phase milestones may persist `progress_percent` in
`state.json` (0–99). The renderer clamps this value and uses it only for display; it never
promotes an unverified state to 100%. This is what makes stable 0/25/50/75 snapshots possible
without letting a stale writer claim completion.

## Fan-out e etapas

Quando `state.json` inclui `lanes` ou `events`, o mesmo evento JSON transporta as lanes de
worktree e o histórico de etapas (`worker_claimed`, `test_gate`, `watcher_challenge`,
`delivery_reconciled`, etc.). O renderer só apresenta esses dados; nunca reconta tarefas nem
deduz uma conclusão a partir do desenho. `--no-animation` faz um snapshot estático (sem ANSI e
sem polling) e `--ascii` troca ícones, barra e spinner por caracteres compatíveis com logs/LLMs.

Exemplo de saída para fan-out:

```text
⚙️ Execução em andamento ·  50%
[████████████░░░░░░░░░░░░] ⠙
▫️ evidence ▫️ watcher ▫️ oracle
ação: worker_claimed → validate
lanes: worker-a 75%/RUNNING · worker-b 25%/BLOCKED
```

Em chat sem streaming, use `--format markdown --once`; em adapters e dashboards, consuma
`--format json --once` e preserve `schema`, `run_id`, `gates`, `lanes` e `events`.
