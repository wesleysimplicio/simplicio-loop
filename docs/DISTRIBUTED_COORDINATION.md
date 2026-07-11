# Distributed coordination contract

This is the transport-neutral contract used when the same backlog is visible to
Codex, Claude, or another runtime on a different device. The backlog file may
live on a shared filesystem today; a remote queue can persist the same fields
without changing the worker protocol.

## Identity

Each participant has a `simplicio.agent-identity/v1` record with:

```json
{
  "agent_id": "agent-codex-a",
  "runtime": "codex",
  "device_id": "device-laptop-a",
  "session_id": "session-2026-07-11",
  "protocol": "simplicio-distributed/v1"
}
```

`agent_id` and `device_id` identify the participant across reconnects;
`session_id` changes on each runtime invocation. The identity is persisted by
`scripts/agent_identity.py` and may be overridden with `SIMPLICIO_*` variables.

## Claim, heartbeat, fencing

```text
next --agent-id ... --runtime ... --session-id ... --device-id ...
  -> lease.worker + lease.identity + monotonic fencing token
heartbeat --item ... --fence ... [same identity]
transition/done --item ... --fence ... [same identity]
```

The queue lock serializes claim/renew/transition. A lease expiry returns the
task to `ready`; a late worker cannot renew or finish it because both the
fencing token and the complete identity must match. Legacy callers remain
compatible when they use `--worker` plus `--fence` without an identity.

## Multi-device safety boundary

The local JSONL backend is safe only when all devices can atomically access the
same filesystem. A future HTTP/SQLite/Redis adapter MUST preserve the same
identity, lease, heartbeat, fencing, and compare-and-swap fields; it must not
silently downgrade to last-write-wins. Runtime adapters should show the
identity in task receipts, never expose another agent's prompt/context, and
re-query the source after reconnecting.

