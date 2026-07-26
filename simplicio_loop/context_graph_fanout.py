"""ContextGraph-aware fan-out planning on top of the existing Map Service.

This module only plans.  It never creates a worktree or grants mutation
authority; callers must bind the returned view handle and authority before
dispatching a wave.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from threading import RLock
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
    schema_identity: str = "simplicio.map-service/v1"
    config_identity: str = ""


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
                identity = self.registry.identity(identity_key)
                config = hashlib.sha256(json.dumps(identity.mapper_config, sort_keys=True).encode()).hexdigest()
                return MapViewHandle("ready", view.cache_key, view.trace_id, view.identity_key, view.tree_hash, view.mode, True, config_identity=config)
        if self.registry is None:
            return MapViewHandle("degraded", fallback=True, reason_code="map_service_unavailable")
        view = self.registry.build_canonical(str(identity_key), tree_hash=str(tree_hash), files=normalized)
        # Acquire the registry-owned handle exactly once for this client.
        view = self.registry.get_view(view.cache_key)
        identity = self.registry.identity(identity_key)
        config = hashlib.sha256(json.dumps(identity.mapper_config, sort_keys=True).encode()).hexdigest()
        handle = MapViewHandle("ready", view.cache_key, view.trace_id, view.identity_key, view.tree_hash, view.mode, config_identity=config)
        self._handles[key] = handle
        return handle

    def request_overlay(self, identity_key: str, *, tree_hash: str, dirty_files: Iterable[str] = ()) -> MapViewHandle:
        if self.registry is None:
            return MapViewHandle("degraded", mode="overlay", fallback=True, reason_code="map_service_unavailable")
        view = self.registry.build_overlay(str(identity_key), tree_hash=str(tree_hash), dirty_files=dirty_files)
        view = self.registry.get_view(view.cache_key)
        identity = self.registry.identity(identity_key)
        config = hashlib.sha256(json.dumps(identity.mapper_config, sort_keys=True).encode()).hexdigest()
        return MapViewHandle("ready", view.cache_key, view.trace_id, view.identity_key, view.tree_hash, view.mode, config_identity=config)

    def release(self, handle: MapViewHandle) -> None:
        if self.registry is not None and handle.cache_key:
            self.registry.release(handle.cache_key)
        for key, value in tuple(self._handles.items()):
            if value.cache_key == handle.cache_key:
                self._handles.pop(key, None)


@dataclass(frozen=True)
class WorktreeMapBinding:
    task_id: str
    owner_id: str
    authority_hash: str
    canonical: MapViewHandle
    overlay: MapViewHandle
    overlay_files: Tuple[str, ...]
    generation: int = 1


class WorktreeMapLeaseManager:
    """Bind canonical+overlay views after authority and release them on every exit."""

    def __init__(self, client: CanonicalMapClient) -> None:
        self.client = client
        self._bindings: Dict[str, WorktreeMapBinding] = {}
        self._lock = RLock()
        self._metrics = {"binds": 0, "cache_hits": 0, "overlay_files": 0,
                         "drift_replans": 0, "releases": 0, "recoveries": 0}

    def bind(self, task: TaskEnvelope, *, owner_id: str, canonical_identity: str,
             canonical_tree_hash: str, canonical_files: Iterable[str],
             worktree_identity: str, overlay_tree_hash: str,
             dirty_files: Iterable[str]) -> WorktreeMapBinding:
        if not task.authority_hash:
            raise ConflictGraphError("mutation authority is required before worktree allocation")
        normalized = tuple(sorted({str(path).replace("\\", "/") for path in dirty_files}))
        with self._lock:
            if task.task_id in self._bindings:
                raise ConflictGraphError("task already has an active map binding")
            canonical = self.client.request_canonical(
                canonical_identity, tree_hash=canonical_tree_hash, files=canonical_files,
            )
            if canonical.status != "ready":
                raise ConflictGraphError(canonical.reason_code or "canonical_map_unavailable")
            try:
                overlay = self.client.request_overlay(
                    worktree_identity, tree_hash=overlay_tree_hash, dirty_files=normalized,
                )
            except BaseException:
                self.client.release(canonical)
                raise
            if overlay.status != "ready":
                self.client.release(canonical)
                raise ConflictGraphError(overlay.reason_code or "overlay_map_unavailable")
            binding = WorktreeMapBinding(
                task.task_id, str(owner_id), task.authority_hash, canonical, overlay, normalized,
            )
            self._bindings[task.task_id] = binding
            self._metrics["binds"] += 1
            self._metrics["cache_hits"] += int(canonical.cache_hit)
            self._metrics["overlay_files"] += len(normalized)
            return binding

    def replan_drift(self, task_id: str, *, overlay_tree_hash: str,
                     dirty_files: Iterable[str]) -> WorktreeMapBinding:
        with self._lock:
            current = self._bindings.get(task_id)
            if current is None:
                raise ConflictGraphError("task has no active map binding")
            self.client.release(current.overlay)
            overlay = self.client.request_overlay(
                current.overlay.identity_key, tree_hash=overlay_tree_hash, dirty_files=dirty_files,
            )
            updated = WorktreeMapBinding(
                current.task_id, current.owner_id, current.authority_hash,
                current.canonical, overlay,
                tuple(sorted({str(path).replace("\\", "/") for path in dirty_files})),
                current.generation + 1,
            )
            self._bindings[task_id] = updated
            self._metrics["drift_replans"] += 1
            return updated

    def release(self, task_id: str) -> bool:
        with self._lock:
            binding = self._bindings.pop(task_id, None)
            if binding is None:
                return False
            self.client.release(binding.overlay)
            self.client.release(binding.canonical)
            self._metrics["releases"] += 1
            return True

    def recover_owner(self, owner_id: str) -> Tuple[str, ...]:
        released = []
        with self._lock:
            for task_id, binding in tuple(self._bindings.items()):
                if binding.owner_id == owner_id and self.release(task_id):
                    released.append(task_id)
            self._metrics["recoveries"] += 1
        return tuple(sorted(released))

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {"schema": SCHEMA, "active": len(self._bindings),
                    "tasks": sorted(self._bindings), "metrics": dict(self._metrics)}


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


__all__ = ["SCHEMA", "CanonicalMapClient", "ConflictGraphError", "MapViewHandle",
           "TaskEnvelope", "WorktreeMapBinding", "WorktreeMapLeaseManager",
           "conflict_graph", "execution_waves"]
