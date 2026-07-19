"""Explicit commit/config/schema invalidation and staleness policy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable, Dict, List, Mapping, Optional


class StaleViewError(RuntimeError):
    """A caller requested a view that is no longer valid under strict policy."""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()


@dataclass
class _View:
    cache_key: str
    identity_key: str
    commit: str
    config_digest: str
    schema: str
    payload: Mapping[str, Any]
    valid: bool = True
    stale_reason: str = ""


class InvalidationCoordinator:
    """Keep staleness explicit and notify subscribers for every invalidation cause."""

    def __init__(self, *, staleness: str = "strict") -> None:
        if staleness not in {"strict", "stale-while-revalidate"}:
            raise ValueError("staleness must be strict or stale-while-revalidate")
        self.staleness = staleness
        self._views: Dict[str, _View] = {}
        self._subscriptions: Dict[str, List[Callable[[Dict[str, Any]], None]]] = {}
        self._lock = RLock()

    def register(self, *, identity_key: str, cache_key: str, commit: str, mapper_config: Any, schema: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self._views[str(cache_key)] = _View(
                str(cache_key), str(identity_key), str(commit), _digest(mapper_config), str(schema), dict(payload)
            )

    def subscribe(self, identity_key: str, callback: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._subscriptions.setdefault(str(identity_key), []).append(callback)

    def refresh(self, *, identity_key: str, commit: str, mapper_config: Any, schema: str) -> List[Dict[str, Any]]:
        """Invalidate all views whose commit, config, or schema no longer matches."""
        current = (str(commit), _digest(mapper_config), str(schema))
        events = []
        with self._lock:
            views = [view for view in self._views.values() if view.identity_key == str(identity_key) and view.valid]
            callbacks = list(self._subscriptions.get(str(identity_key), []))
            for view in views:
                expected = (view.commit, view.config_digest, view.schema)
                if expected == current:
                    continue
                reasons = []
                if view.commit != current[0]: reasons.append("commit")
                if view.config_digest != current[1]: reasons.append("config")
                if view.schema != current[2]: reasons.append("schema")
                view.valid = False
                view.stale_reason = "+".join(reasons)
                event = {
                    "schema": "simplicio.map-service-invalidation/v1", "event": "invalidate",
                    "identity_key": view.identity_key, "cache_key": view.cache_key,
                    "reason": view.stale_reason, "stale": True,
                }
                events.append(event)
        for event in events:
            for callback in callbacks:
                callback(dict(event))
        return events

    def get(self, cache_key: str, *, allow_stale: bool = False) -> Dict[str, Any]:
        with self._lock:
            view = self._views.get(str(cache_key))
            if view is None:
                raise KeyError(cache_key)
            if not view.valid:
                if self.staleness != "stale-while-revalidate" or not allow_stale:
                    raise StaleViewError("view is stale: %s" % view.stale_reason)
                return {**dict(view.payload), "cache_key": view.cache_key, "stale": True, "stale_reason": view.stale_reason}
            return {**dict(view.payload), "cache_key": view.cache_key, "stale": False}

    def status(self) -> Dict[str, int]:
        with self._lock:
            return {"views": len(self._views), "valid": sum(view.valid for view in self._views.values()), "stale": sum(not view.valid for view in self._views.values())}
