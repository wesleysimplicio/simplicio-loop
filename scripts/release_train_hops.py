#!/usr/bin/env python3
"""Idempotent release-train hop planner for Loop #558."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from typing import Any, Iterable, Mapping

SCHEMA = "simplicio.ecosystem-release-hops/v1"
DEFAULT_EDGES = (
    ("simplicio-mapper", "simplicio-dev-cli"),
    ("simplicio-dev-cli", "simplicio-loop"),
    ("simplicio-loop", "simplicio-runtime"),
    ("simplicio-loop", "simplicio-loop-oss"),
    ("simplicio-loop", "simplicio-loop-marketing"),
    ("simplicio-runtime", "simplicio-agent"),
    ("simplicio-runtime", "simplicio-code"),
)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_manifests(manifests: Iterable[Mapping[str, Any]]) -> list[str]:
    errors: list[str] = []
    seen = set()
    for manifest in manifests:
        component = manifest.get("component")
        if not component or component in seen:
            errors.append(f"duplicate_or_missing_component:{component}")
        seen.add(component)
        for key in ("version", "commit", "artifacts", "compatibility"):
            if not manifest.get(key):
                errors.append(f"{component}:missing:{key}")
        for artifact in manifest.get("artifacts", []):
            if not artifact.get("digest") or not artifact.get("signature"):
                errors.append(f"{component}:artifact_unverified")
    return sorted(set(errors))


def topo_order(components: Iterable[str], edges: Iterable[tuple[str, str]] = DEFAULT_EDGES) -> list[str]:
    nodes = set(components)
    for parent, child in edges:
        nodes.update((parent, child))
    indegree = {node: 0 for node in nodes}
    outgoing: dict[str, set[str]] = defaultdict(set)
    for parent, child in edges:
        if child not in outgoing[parent]:
            outgoing[parent].add(child)
            indegree[child] += 1
    queue = deque(sorted(node for node, degree in indegree.items() if degree == 0))
    ordered = []
    while queue:
        node = queue.popleft()
        ordered.append(node)
        for child in sorted(outgoing[node]):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(ordered) != len(nodes):
        raise ValueError("dependency_cycle")
    return ordered


def plan_event(event_id: str, manifests: Iterable[Mapping[str, Any]], *, seen_events: set[str] | None = None,
               edges: Iterable[tuple[str, str]] = DEFAULT_EDGES) -> dict[str, Any]:
    if not event_id:
        raise ValueError("event_id is required")
    seen_events = set(seen_events or set())
    if event_id in seen_events:
        return {"schema": SCHEMA, "event_id": event_id, "status": "duplicate", "hops": [], "next_action": "noop"}
    manifests = list(manifests)
    errors = validate_manifests(manifests)
    if errors:
        return {"schema": SCHEMA, "event_id": event_id, "status": "blocked", "errors": errors, "hops": []}
    available = {str(item["component"]) for item in manifests}
    order = topo_order(available, edges)
    hops = [{"component": component, "action": "open_or_update_bump", "requires": "verified_artifact"} for component in order if component in available]
    result = {"schema": SCHEMA, "event_id": event_id, "status": "planned", "hops": hops, "graph_digest": _digest(list(edges))}
    result["plan_digest"] = _digest(result)
    return result
