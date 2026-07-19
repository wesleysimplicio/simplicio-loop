"""On-disk persistence for map-service identities, views, and single-flight leases.

A restart (new process, new MapServiceRegistry/SingleFlightMapStore instance) must not
forget which builds are already owned, which snapshot a cache key maps to, or how many
active handles (leases) are pinning a view. ``save`` serializes that state; ``load``
rehydrates a fresh registry+store pair from it so a new instance pointed at the same
on-disk path resumes exactly where the previous one left off.
"""

import json
from pathlib import Path
from typing import Any, Dict, Tuple

from .map_service import MapServiceRegistry, MapView, RepositoryIdentity
from .map_service_single_flight import SingleFlightMapStore

SNAPSHOT_SCHEMA = "simplicio.map-service-snapshot/v1"


def snapshot(registry: MapServiceRegistry, store: "SingleFlightMapStore | None" = None) -> Dict[str, Any]:
    with registry._lock:
        identities = [identity.to_dict() for identity in registry._identities.values()]
        views = [view.to_dict() for view in registry._views.values()]
        version = registry._version
    completed = []
    if store is not None:
        with store._lock:
            for key, cache_key in store._completed.items():
                identity_key, mode, tree_hash, files = key
                completed.append({
                    "identity_key": identity_key,
                    "mode": mode,
                    "tree_hash": tree_hash,
                    "files": list(files),
                    "cache_key": cache_key,
                })
    return {
        "schema": SNAPSHOT_SCHEMA,
        "version": version,
        "identities": identities,
        "views": views,
        "completed": completed,
    }


def save(path: Any, registry: MapServiceRegistry, store: "SingleFlightMapStore | None" = None) -> None:
    data = snapshot(registry, store)
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def load(path: Any) -> Tuple[MapServiceRegistry, SingleFlightMapStore]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError("not a map-service snapshot")
    registry = MapServiceRegistry()
    for identity_dict in data["identities"]:
        identity = RepositoryIdentity(
            repository=identity_dict["repository"],
            canonical_root=identity_dict["canonical_root"],
            default_branch=identity_dict["default_branch"],
            worktree_root=identity_dict["worktree_root"],
            base_sha=identity_dict["base_sha"],
            dirty=identity_dict["dirty"],
            dirty_fingerprint=identity_dict["dirty_fingerprint"],
            mapper_config=identity_dict["mapper_config"],
        )
        registry.register(identity)
    with registry._lock:
        for view_dict in data["views"]:
            view = MapView(
                identity_key=view_dict["identity_key"],
                mode=view_dict["mode"],
                tree_hash=view_dict["tree_hash"],
                files=tuple(view_dict["files"]),
                cache_key=view_dict["cache_key"],
                trace_id=view_dict["trace_id"],
                version=view_dict["version"],
                dirty=view_dict["dirty"],
                valid=view_dict["valid"],
                references=view_dict["references"],
            )
            registry._views[view.cache_key] = view
        registry._version = int(data["version"])
    store = SingleFlightMapStore(registry)
    with store._lock:
        for record in data.get("completed", []):
            key = SingleFlightMapStore._key(
                record["identity_key"], record["mode"], record["tree_hash"], record["files"],
            )
            store._completed[key] = record["cache_key"]
    return registry, store
