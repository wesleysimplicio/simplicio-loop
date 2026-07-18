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

## Rollback

Stop the daemon, remove only the stale lock after confirming its PID is dead, and unset the Hub
endpoint/feature flag in the caller. No job state is treated as delivered until the caller receives
the versioned response envelope.
