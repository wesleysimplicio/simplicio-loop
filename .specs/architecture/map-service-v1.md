# Map Service protocol v1

This contract is the transport-neutral boundary between the Loop Hub and map-service
clients. Every request carries `schema: simplicio.map-service/v1` and `version: 1`.
Unknown operations, missing required fields, and incompatible versions fail closed with
an error object containing `code`, `message`, and `details`.

| Operation | Required payload | Result responsibility |
| --- | --- | --- |
| `resolve_repo` | `path` | Resolve one registered repository/worktree identity. |
| `get_view` | `cache_key` | Acquire a valid view handle. |
| `build_canonical` | `identity_key`, `tree_hash` | Build/reuse the default-branch view. |
| `build_overlay` | `identity_key`, `tree_hash` | Build/reuse a dirty-worktree overlay. |
| `subscribe` | `identity_key` | Register invalidation notifications. |
| `invalidate` | `identity_key` | Invalidate affected views with a reason. |
| `release` | `cache_key` | Release one acquired view reference. |
| `gc` | none | Reclaim invalidated, unreferenced snapshots. |

The identity key is derived from repository, canonical root, worktree root, default branch,
HEAD/base SHA, dirty fingerprint, and mapper configuration. Canonical and overlay modes are
separate cache dimensions. Version negotiation is exact in v1; clients must upgrade instead
of silently accepting a lower contract.

The executable validator lives in `simplicio_loop.map_service_protocol` and is intentionally
independent of Unix sockets, named pipes, or a particular mapper implementation.
