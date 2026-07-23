"""ContextGraph-aware fan-out planning on top of the existing Map Service.

This module only plans.  It never creates a worktree or grants mutation
authority; callers must bind the returned view handle and authority before
dispatching a wave.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from .map_service import MapServiceError, MapServiceRegistry

SCHEMA = "simplicio.context-graph-fanout/v1"


class ConflictGraphError(ValueError):
    """The task graph cannot be scheduled safely."""


@dataclass(frozen=True)
class TaskEnvelope:
    task_id: str
    mutation_targets: Tuple[str, ...] = ()
    symbols: Tuple[str, ...] = ()
    reverse_dependencies: Tuple[str, ...] = ()
    tests: Tuple[str, ...] = ()
    resources: Tuple[str, ...] = ()
    depends_on: Tuple[str, ...] = ()
    authority_hash: str = ""

    def __post_init__(self) -> None:
        if not str(self.task_id).strip():
            raise ValueError("task_id is required")
        object.__setattr__(self, "task_id", str(self.task_id).strip())
        for name in ("mutation_targets", "symbols", "reverse_dependencies", "tests", "resources", "depends_on"):
            values = tuple(sorted({str(value).replace("\\", "/").strip() for value in getattr(self, name) if str(value).strip()}))
            object.__setattr__(self, name, values)
        object.__setattr__(self, "authority_hash", str(self.authority_hash or "").strip())

    @property
    def fingerprint(self) -> str:
        payload = {name: getattr(self, name) for name in (
            "task_id", "mutation_targets", "symbols", "reverse_dependencies",
            "tests", "resources", "depends_on", "authority_hash",
        )}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class MapViewHandle:
    status: str
    cache_key: str = ""
    trace_id: str = ""
    identity_key: str = ""
    tree_hash: str = ""
    mode: str = "canonical"
    cache_hit: bool = False
    fallback: bool = False
    reason_code: str = ""


@dataclass
class CanonicalMapClient:
    """Small lifecycle adapter; the registry remains the owner of views."""

    registry: Optional[MapServiceRegistry] = None
    _handles: Dict[Tuple[str, str, Tuple[str, ...]], MapViewHandle] = field(default_factory=dict)

    def request_canonical(self, identity_key: str, *, tree_hash: str, files: Iterable[str] = ()) -> MapViewHandle:
        normalized = tuple(sorted({str(path).replace("\\", "/").strip() for path in files if str(path).strip()}))
        key = (str(identity_key), str(tree_hash), normalized)
        existing = self._handles.get(key)
        if existing and self.registry is not None:
            try:
                view = self.registry.get_view(existing.cache_key)
            except MapServiceError:
                self._handles.pop(key, None)
            else:
                return MapViewHandle("ready", view.cache_key, view.trace_id, view.identity_key, view.tree_hash, view.mode, True)
        if self.registry is None:
            return MapViewHandle("degraded", fallback=True, reason_code="map_service_unavailable")
        view = self.registry.build_canonical(str(identity_key), tree_hash=str(tree_hash), files=normalized)
        # Acquire the registry-owned handle exactly once for this client.
        view = self.registry.get_view(view.cache_key)
        handle = MapViewHandle("ready", view.cache_key, view.trace_id, view.identity_key, view.tree_hash, view.mode)
        self._handles[key] = handle
        return handle

    def request_overlay(self, identity_key: str, *, tree_hash: str, dirty_files: Iterable[str] = ()) -> MapViewHandle:
        if self.registry is None:
            return MapViewHandle("degraded", mode="overlay", fallback=True, reason_code="map_service_unavailable")
        view = self.registry.build_overlay(str(identity_key), tree_hash=str(tree_hash), dirty_files=dirty_files)
        view = self.registry.get_view(view.cache_key)
        return MapViewHandle("ready", view.cache_key, view.trace_id, view.identity_key, view.tree_hash, view.mode)

    def release(self, handle: MapViewHandle) -> None:
        if self.registry is not None and handle.cache_key:
            self.registry.release(handle.cache_key)
        for key, value in tuple(self._handles.items()):
            if value.cache_key == handle.cache_key:
                self._handles.pop(key, None)


def conflict_graph(tasks: Sequence[TaskEnvelope]) -> Dict[str, Dict[str, Any]]:
    """Return deterministic directed edges and typed conflict reasons."""
    by_id = {task.task_id: task for task in tasks}
    if len(by_id) != len(tasks):
        raise ConflictGraphError("duplicate task id")
    graph = {task.task_id: {"after": [], "reasons": {}} for task in sorted(tasks, key=lambda item: item.task_id)}

    def add(before: str, after: str, reason: str, confidence: str, hard: bool) -> None:
        if before == after:
            return
        row = graph[after]
        if before not in row["after"]:
            row["after"].append(before)
        row["reasons"].setdefault(before, []).append({"code": reason, "confidence": confidence, "hard": hard})

    for task in tasks:
        for dependency in task.depends_on:
            if dependency not in by_id:
                raise ConflictGraphError(f"unknown dependency {dependency} for {task.task_id}")
            add(dependency, task.task_id, "explicit_dependency", "certain", True)
        for dependency in task.reverse_dependencies:
            if dependency in by_id:
                add(dependency, task.task_id, "reverse_dependency", "high", True)
    ordered = sorted(tasks, key=lambda item: item.task_id)
    for index, left in enumerate(ordered):
        for right in ordered[index + 1:]:
            shared = set(left.mutation_targets) & set(right.mutation_targets)
            symbols = set(left.symbols) & set(right.symbols)
            tests = set(left.tests) & set(right.tests)
            resources = set(left.resources) & set(right.resources)
            reasons = []
            if shared:
                reasons.append(("shared_mutation_target", "certain", True))
            if symbols:
                reasons.append(("shared_symbol", "high", True))
            if tests:
                reasons.append(("test_contention", "medium", False))
            if resources:
                reasons.append(("resource_contention", "medium", False))
            for reason, confidence, hard in reasons:
                add(left.task_id, right.task_id, reason, confidence, hard)
    for value in graph.values():
        value["after"].sort()
        for reasons in value["reasons"].values():
            reasons.sort(key=lambda item: (item["code"], item["confidence"]))
    return graph


def execution_waves(tasks: Sequence[TaskEnvelope], *, capacity: int = 4) -> Dict[str, Any]:
    """Topologically schedule conflict edges; cycles fail closed."""
    if capacity < 1:
        raise ValueError("capacity must be positive")
    graph = conflict_graph(tasks)
    remaining = {task.task_id for task in tasks}
    waves = []
    while remaining:
        ready = sorted(task_id for task_id in remaining if not (set(graph[task_id]["after"]) & remaining))
        if not ready:
            raise ConflictGraphError("ambiguous or cyclic high-risk conflict")
        wave = ready[:capacity]
        waves.append(wave)
        remaining.difference_update(wave)
    return {"schema": SCHEMA, "waves": waves, "graph": graph, "capacity": capacity,
            "task_count": len(tasks), "degraded": False}


__all__ = ["SCHEMA", "CanonicalMapClient", "ConflictGraphError", "MapViewHandle", "TaskEnvelope", "conflict_graph", "execution_waves"]
