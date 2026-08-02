# `simplicio.stack-lock/v1`

The Loop now has a deterministic stack-lock primitive for issue #1032. Callers
provide observations from installed Mapper/Fast/Dev CLI/Runtime components; the
primitive canonicalizes component order, hashes artifacts when the executable
is a regular file, records capabilities and freezes the route before an effect.

`standalone` is valid without Runtime. `runtime-backed` fails closed unless an
available Runtime observation is present. `verify_unchanged` rejects artifact,
capability, version, component, run-id, or route drift after freeze.

The installed CLI accepts a JSON observation file and persists the lock
atomically without invoking commands or installing packages:

```text
simplicio-loop stack lock \
  --components components.json \
  --route standalone \
  --run-id run-123 \
  --output .simplicio/orchestrator/stack-lock.json

simplicio-loop stack verify \
  --lock .simplicio/orchestrator/stack-lock.json \
  --components components.json
```

An existing lock is immutable: writing a different hash is blocked. Verification
recomputes the canonical hash and then compares the current artifact, version,
capability and route observations. Contract Registry integration, cross-platform
installed discovery, upgrade/rollback diagnostics, automatic runner wiring and
Resource Fabric takeover remain residual work for #1032.
