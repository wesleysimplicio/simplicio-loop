# Loop-owned agent slots

`simplicio_loop.agent_slots.AgentSlotRegistry` is the Loop-owned capacity and
lifecycle boundary for multi-agent lanes. It is deliberately independent of
any external coordinator: an adapter can call `acquire`, `start`,
`close_agent`, `update_blockers`, and `reclaim`, while Loop remains the source
of truth for capacity and receipts.

The default capacity is six. `pending` and `running` consume capacity;
`completed` and `shutdown` release it immediately while remaining visible for
audit; `reclaimable` is a derived, idempotent post-cleanup capability on those
terminal records. Status returns counts
for all states, current holders, available capacity, and blockers for
descendants, worktrees, or leases.

The CLI surface is JSON-first:

```text
python -m simplicio_loop.cli agent-slots status --db .simplicio/orchestrator/agent-slots.sqlite
python -m simplicio_loop.cli agent-slots acquire agent-a --db .simplicio/orchestrator/agent-slots.sqlite
python -m simplicio_loop.cli agent-slots start agent-a --db .simplicio/orchestrator/agent-slots.sqlite
python -m simplicio_loop.cli agent-slots close agent-a --status completed --db .simplicio/orchestrator/agent-slots.sqlite
python -m simplicio_loop.cli agent-slots reclaim --db .simplicio/orchestrator/agent-slots.sqlite
```

`spawn_batch` is the programmatic seam for a real adapter. It retries a failed
spawn at most `retry_limit` times, reuses the same logical agent record, and
never creates a duplicate slot. No operation starts a local LLM.
