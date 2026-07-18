# Simplicio Loop Hub local IPC runbook

The Hub is opt-in. Existing callers remain standalone unless they explicitly connect to a Hub
endpoint.

## Transport and security

- POSIX uses a Unix domain socket with mode `0600`.
- Windows uses a named pipe (`AF_PIPE`) through the Python standard library.
- TCP is not selected implicitly; a future TCP fallback must be explicit and authenticated.
- Requests use `simplicio.hub-ipc/v1` and are rejected when schema or version differs.
- The lock contains the owner PID. A live owner blocks a second daemon; a dead or corrupt lock is
  reclaimed deterministically.

## Lifecycle

```powershell
simplicio-hub serve --lock <path> --endpoint <endpoint>
simplicio-hub doctor --lock <path> --endpoint <endpoint>
```

`doctor` performs a real `ping` over the selected endpoint and reports lock ownership and
reachability. `HubSocketServer.shutdown()` is idempotent and removes the Unix socket and singleton
lock. If the Hub is unavailable, callers must keep using their standalone adapter.

## Supervisor execution

The versioned IPC method `execute` accepts a `process_spec` using
`simplicio.process-spec/v1` and returns a bounded `simplicio.process-result/v1`. The payload is
argv-only: `shell=true`, unknown fields, invalid cwd roots, and environment keys outside the
allowlist are rejected. The Hub invokes the compiled Rust/Tokio adapter when it is available and
reports `backend: "rust"`; otherwise it deliberately uses the safe Python adapter and reports
`backend: "python-fallback"`. The fallback preserves standalone compatibility but does not claim
Rust-level cross-platform resource controls. cgroups, Windows Job Objects, quotas, and full Hub
queue integration remain separate supervisor work.

## Scheduler backpressure (degradation signal)

`submit` now routes through `FairScheduler.enqueue()` before the job is persisted. When a
client/workspace/global queue quota is exceeded, the daemon raises `HubBackpressureError`
(`hub_daemon.py`) instead of accepting the job; the socket transport turns that into
`{"ok": false, "error": "..."}` on the wire (see `HubSocketServer._dispatch`), so a caller getting
`ok: false` on `submit` should check for a quota message before treating it as a generic protocol
failure. `scheduler_status` returns the live `FairScheduler.status()` snapshot (queue depth per
client, inflight counts) so an operator can poll it to see the quota being approached before jobs
start being rejected. A restarted daemon repopulates the scheduler from `list_queued_scheduling_metadata()`, so a
crash-restart does not silently drop fairness bookkeeping for still-queued jobs.

## Rollback

Stop the daemon, remove only the stale lock after confirming its PID is dead, and unset the Hub
endpoint/feature flag in the caller. No job state is treated as delivered until the caller receives
the versioned response envelope.
