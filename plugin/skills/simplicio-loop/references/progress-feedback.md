# Progress feedback nucleus (issue #298, EPIC #296) — full detail

`scripts/loop_progress.py` is the ONE place "em que etapa estamos / quanto falta" is computed and
persisted. Every stage of the loop calls `emit` at begin/end; nothing else computes a %. All
sister issues (#299-#304 — intake, turn, delivery, transcript, runtimes, gates) are pure
instrumentation callers of this worker.

## State on disk (new, `.orchestrator/loop/`, override the directory with `$SIMPLICIO_PROGRESS_DIR`)

| File | Role |
|---|---|
| `progress.jsonl` | append-only events, one line per `emit` call, locked via `scripts/_locked_append.py` (same discipline as `journal.jsonl`) |
| `progress.json` | derived snapshot (last phase/step, %, active item, timestamps) — **never authoritative**, see invariant below |
| `PROGRESS.md` | human render: turn-header line + text progress bar + table + last 5 transitions — the universal surface for hosts without hooks |

## Invariant — projection, never authority (AC7)

`status` and `render` always **recompute** `pct_*` fresh from `task_backlog.py`'s items +
`task_anchor.py`'s criteria + the event trail's turn position. They never read `progress.json*` to
decide a number — deleting the snapshot before `status` yields the identical %.

## Canonical stage machine (frozen, importable)

```python
PHASES = ["F0", "F1", "F2", "F3"]   # intake, execução, entrega, encerramento
STEPS  = ["preflight", "survey", "triage", "decide", "operate", "watcher", "journal",
          "evidence", "refeed_exit"]
```

This is exactly the turn described in `SKILL.md` § Bound operators: `preflight -> survey -> triage
-> DECIDE -> operate -> watcher-gate -> promise`, bracketed by `evidence` (PR/issue delivery) and
`refeed_exit` (the re-feed/exit decision).

## Event schema (one JSONL line)

```json
{
  "ts": "<ISO-8601>", "iteration": 7, "phase": "F1", "step": "operate",
  "step_index": 5, "steps_total": 9, "status": "begin|end|blocked|skipped",
  "outcome": "pass|fail|blocked|null", "item_id": "T3", "detail": "<short string>",
  "source": "task_backlog.py|watcher_verify.py|...",
  "pct_item": 0.33, "pct_overall": 0.42, "rebaseline": false
}
```

## % formula (deterministic, no fabricated numbers)

- `pct_item = acs_verificados / acs_totais` — the active item's AC coverage, read from the anchor.
- **drain** (a backlog with >=1 item exists): `pct_overall = (itens_done + pct_item_do_item_ativo) / itens_totais`.
- **converge** (no backlog, an anchor exists): `pct_overall = pct_item * 0.9 + (step_index / steps_total) * 0.1` — the step fraction gives visible in-turn motion without ever dominating the AC-gated number.
- **unknown** (neither source on disk): `pct_overall` is `None`; every render prints `UNVERIFIED|pct=?`.

## CLI

```bash
python3 scripts/loop_progress.py emit --step operate --status begin --item T3 \
    [--detail "..."] [--outcome pass] [--iteration 7] [--source watcher_verify.py] [--cap 24]
python3 scripts/loop_progress.py status [--json]
python3 scripts/loop_progress.py render [--turn-header|--full] [--cap 24]
python3 scripts/loop_progress.py selftest
```

`render --turn-header` (the contract #302 requires in every turn):

```
MEASURED|[simplicio-loop] fase F1 · etapa 5/9 operate · item T3 (2/5 itens) · ACs 1/3 · 42% geral · iter 7/24
```

## Fail-open

Every error path (missing/corrupt backlog, missing/corrupt anchor, truncated/corrupt
`progress.jsonl`, a `_locked_append` lock timeout) ends in exit 0 with `UNVERIFIED|...` output —
never an uncaught exception, never a fabricated %. Proven by `scripts/loop_progress.py selftest`
and `tests/test_loop_progress.py`.
