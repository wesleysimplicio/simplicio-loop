# `simplicio.stack-lock/v1`

The Loop now has a deterministic, read-only stack-lock primitive for issue
#1032. Callers provide observations from installed Mapper/Fast/Dev CLI/Runtime
components; the primitive canonicalizes component order, hashes artifacts when
the executable is a regular file, records capabilities and freezes the route
before an effect.

`standalone` is valid without Runtime. `runtime-backed` fails closed unless an
available Runtime observation is present. `verify_unchanged` rejects artifact,
capability, version, component, run-id, or route drift after freeze.

This module does not invoke commands, install packages, persist the lock, or
perform an effect. User-facing `stack lock`/`stack verify` commands, Contract
Registry integration, cross-platform installed discovery, upgrade/rollback
diagnostics, and Resource Fabric takeover remain residual work for #1032.
