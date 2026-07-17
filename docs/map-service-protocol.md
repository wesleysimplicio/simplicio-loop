# Simplicio Map Service protocol

Schema: `simplicio.map-service/v1`.

The in-process `MapServiceRegistry` is the standalone fallback and the
contract boundary for the future Hub transport. It does not make network
calls, start watchers, or assume a particular IDE.

## Identity

A repository identity contains:

- repository owner/name;
- canonical checkout root;
- default branch;
- optional worktree root;
- base commit SHA and dirty state.

Canonical identities resolve the default checkout. A worktree identity resolves
only its own worktree root, so a dirty overlay cannot shadow the canonical
checkout. Equal root collisions fail closed.

## Operations

`resolve_repo`, `get_view`, `build_canonical`, `build_overlay`,
`subscribe`, `invalidate`, `release`, and `gc` are the versioned
operations exposed by the registry.

Canonical and overlay views receive distinct content-addressed cache keys and
trace IDs. A view becomes invalid after source change; consumers must release
their handles before garbage collection removes it.

## Limits and rollout

This slice is deliberately bounded: it provides an in-process registry,
content-addressed view metadata, reference counting, and synchronous
invalidation callbacks. Single-flight ownership, watcher quotas, persistence,
backpressure, and remote IPC remain follow-up slices #512 and #513.

The standalone registry is the rollback path. A caller can stop using the
registry and continue with its existing mapper invocation without changing
repository identity or GitHub coordination.
