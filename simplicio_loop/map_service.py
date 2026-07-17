"""Versioned repository identity and canonical/overlay map-service protocol."""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

PROTOCOL_SCHEMA = "simplicio.map-service/v1"
PROTOCOL_VERSION = 1
MODES = frozenset(("canonical", "overlay"))


class MapServiceError(ValueError):
    """Base error for invalid map-service requests."""


class AmbiguousRepositoryError(MapServiceError):
    """Raised when a path matches equally specific repository identities."""


class UnknownRepositoryError(MapServiceError):
    """Raised when an identity or path is not registered."""


def _normalize(path: str) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve(strict=False)))


def _contains(root: str, path: str) -> bool:
    try:
        return os.path.commonpath((root, path)) == root
    except ValueError:
        return False


def _digest(payload: Any) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RepositoryIdentity:
    """Stable identity for a repository or one specific worktree."""

    repository: str
    canonical_root: str
    default_branch: str = "main"
    worktree_root: Optional[str] = None
    base_sha: str = ""
    dirty: bool = False

    def __post_init__(self) -> None:
        repository = str(self.repository).strip()
        canonical_root = _normalize(self.canonical_root)
        worktree_root = (
            _normalize(self.worktree_root) if self.worktree_root else None
        )
        branch = str(self.default_branch).strip() or "main"
        if not repository:
            raise MapServiceError("repository is required")
        if not str(self.base_sha).strip():
            raise MapServiceError("base_sha is required")
        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "canonical_root", canonical_root)
        object.__setattr__(self, "worktree_root", worktree_root)
        object.__setattr__(self, "default_branch", branch)

    @property
    def key(self) -> str:
        return _digest({
            "repository": self.repository,
            "canonical_root": self.canonical_root,
            "default_branch": self.default_branch,
            "worktree_root": self.worktree_root,
            "base_sha": self.base_sha,
        })

    @property
    def cache_namespace(self) -> str:
        return "repo:" + self.repository + ":" + self.default_branch

    def matches(self, path: str) -> Tuple[bool, int]:
        candidate = _normalize(path)
        roots = [self.worktree_root] if self.worktree_root else [self.canonical_root]
        matches = [(root, len(root)) for root in roots if _contains(root, candidate)]
        if not matches:
            return False, -1
        return True, max(length for _, length in matches)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repository": self.repository,
            "canonical_root": self.canonical_root,
            "default_branch": self.default_branch,
            "worktree_root": self.worktree_root,
            "base_sha": self.base_sha,
            "dirty": self.dirty,
            "identity_key": self.key,
        }


@dataclass
class MapView:
    """A content-addressed view returned by canonical/overlay builds."""

    identity_key: str
    mode: str
    tree_hash: str
    files: Tuple[str, ...]
    cache_key: str
    trace_id: str
    version: int
    dirty: bool = False
    valid: bool = True
    references: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "identity_key": self.identity_key,
            "mode": self.mode,
            "tree_hash": self.tree_hash,
            "files": list(self.files),
            "cache_key": self.cache_key,
            "trace_id": self.trace_id,
            "version": self.version,
            "dirty": self.dirty,
            "valid": self.valid,
            "references": self.references,
        }


class MapServiceRegistry:
    """Thread-safe registry for repository identities and view lifecycle."""

    def __init__(self) -> None:
        self._identities: Dict[str, RepositoryIdentity] = {}
        self._views: Dict[str, MapView] = {}
        self._subscriptions: Dict[str, Tuple[str, Callable[[Dict[str, Any]], None]]] = {}
        self._version = 0
        self._lock = RLock()

    @staticmethod
    def protocol() -> Dict[str, Any]:
        return {
            "schema": PROTOCOL_SCHEMA,
            "version": PROTOCOL_VERSION,
            "operations": [
                "resolve_repo", "get_view", "build_canonical", "build_overlay",
                "subscribe", "invalidate", "release", "gc",
            ],
            "identity_fields": [
                "repository", "canonical_root", "default_branch",
                "worktree_root", "base_sha", "dirty",
            ],
            "guarantees": [
                "canonical and overlay cache keys are distinct",
                "ambiguous path matches fail closed",
                "invalidated views are not returned by get_view",
                "standalone registry has no network dependency",
            ],
        }

    def register(self, identity: RepositoryIdentity) -> str:
        with self._lock:
            for existing in self._identities.values():
                same_root = (
                    existing.canonical_root == identity.canonical_root
                    and existing.worktree_root == identity.worktree_root
                )
                if same_root and existing.key != identity.key:
                    raise AmbiguousRepositoryError(
                        "repository identity collision for the same root/worktree"
                    )
            self._identities[identity.key] = identity
            return identity.key

    def identity(self, identity_key: str) -> RepositoryIdentity:
        try:
            return self._identities[identity_key]
        except KeyError as exc:
            raise UnknownRepositoryError(identity_key) from exc

    def resolve_repo(self, path: str) -> RepositoryIdentity:
        with self._lock:
            matches = []
            for identity in self._identities.values():
                found, specificity = identity.matches(path)
                if found:
                    matches.append((specificity, identity))
            if not matches:
                raise UnknownRepositoryError(str(path))
            best = max(item[0] for item in matches)
            candidates = [item[1] for item in matches if item[0] == best]
            if len(candidates) != 1:
                raise AmbiguousRepositoryError(str(path))
            return candidates[0]

    def _view(
        self,
        identity_key: str,
        mode: str,
        tree_hash: str,
        files: Iterable[str],
        trace_id: Optional[str],
        dirty: bool,
    ) -> MapView:
        if mode not in MODES:
            raise MapServiceError("mode must be canonical or overlay")
        identity = self.identity(identity_key)
        normalized_files = tuple(sorted({_normalize(path) for path in files}))
        self._version += 1
        cache_key = _digest({
            "identity_key": identity.key,
            "mode": mode,
            "tree_hash": str(tree_hash),
            "files": normalized_files,
        })
        view = MapView(
            identity_key=identity.key,
            mode=mode,
            tree_hash=str(tree_hash),
            files=normalized_files,
            cache_key=cache_key,
            trace_id=str(trace_id or _digest({"cache_key": cache_key})[:16]),
            version=self._version,
            dirty=bool(dirty),
        )
        self._views[cache_key] = view
        return view

    def build_canonical(
        self,
        identity_key: str,
        *,
        tree_hash: str,
        files: Iterable[str] = (),
        trace_id: Optional[str] = None,
    ) -> MapView:
        with self._lock:
            identity = self.identity(identity_key)
            return self._view(identity.key, "canonical", tree_hash, files, trace_id, False)

    def build_overlay(
        self,
        identity_key: str,
        *,
        tree_hash: str,
        dirty_files: Iterable[str] = (),
        trace_id: Optional[str] = None,
    ) -> MapView:
        with self._lock:
            identity = self.identity(identity_key)
            if not identity.worktree_root:
                raise MapServiceError("overlay requires a worktree_root")
            return self._view(identity.key, "overlay", tree_hash, dirty_files, trace_id, True)

    def get_view(self, cache_key: str, *, acquire: bool = True) -> MapView:
        with self._lock:
            view = self._views.get(str(cache_key))
            if view is None or not view.valid:
                raise UnknownRepositoryError("map view is missing or invalid")
            if acquire:
                view.references += 1
            return view

    def subscribe(
        self, identity_key: str, callback: Callable[[Dict[str, Any]], None]
    ) -> str:
        with self._lock:
            self.identity(identity_key)
            token = _digest({
                "identity_key": identity_key,
                "subscription": len(self._subscriptions) + 1,
            })[:24]
            self._subscriptions[token] = (identity_key, callback)
            return token

    def release(self, cache_key: str) -> None:
        with self._lock:
            view = self._views.get(str(cache_key))
            if view is not None:
                view.references = max(0, view.references - 1)

    def invalidate(self, identity_key: str, *, reason: str = "source_changed") -> List[str]:
        events = []
        with self._lock:
            self.identity(identity_key)
            for view in self._views.values():
                if view.identity_key != identity_key or not view.valid:
                    continue
                view.valid = False
                events.append({
                    "schema": PROTOCOL_SCHEMA,
                    "event": "invalidate",
                    "identity_key": identity_key,
                    "cache_key": view.cache_key,
                    "reason": str(reason),
                    "trace_id": view.trace_id,
                })
            callbacks = [
                callback
                for subscribed_key, callback in self._subscriptions.values()
                if subscribed_key == identity_key
            ]
        for event in events:
            for callback in callbacks:
                callback(dict(event))
        return [event["cache_key"] for event in events]

    def gc(self) -> List[str]:
        with self._lock:
            removed = [
                cache_key for cache_key, view in self._views.items()
                if not view.valid and view.references == 0
            ]
            for cache_key in removed:
                del self._views[cache_key]
            return sorted(removed)

    def release_all(self, identity_key: str) -> int:
        with self._lock:
            self.identity(identity_key)
            count = 0
            for view in self._views.values():
                if view.identity_key == identity_key:
                    count += view.references
                    view.references = 0
            return count
