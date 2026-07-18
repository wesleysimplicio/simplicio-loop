"""Debounced, quota-bounded map watchers over the standalone registry."""

import time
from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable, Dict, Iterable, List, Optional

from .map_service import MapServiceRegistry
from .map_service_single_flight import SingleFlightMapStore


class WatcherError(RuntimeError):
    """Base watcher error."""


class WatcherQuotaError(WatcherError):
    """Raised when watcher quota is exceeded."""


class WatcherBackpressureError(WatcherError):
    """Raised when pending-event quota is exceeded."""


@dataclass
class _Watcher:
    token: str
    identity_key: str
    callback: Callable[[Dict[str, Any]], None]
    debounce_seconds: float


class MapWatcherManager:
    """Own one bounded watcher per identity and coalesce file events."""

    def __init__(
        self,
        registry: MapServiceRegistry,
        store: Optional[SingleFlightMapStore] = None,
        *,
        max_watchers: int = 64,
        max_pending: int = 256,
    ) -> None:
        if max_watchers < 1 or max_pending < 1:
            raise ValueError("watcher quotas must be positive")
        self.registry = registry
        self.store = store
        self.max_watchers = max_watchers
        self.max_pending = max_pending
        self._watchers: Dict[str, _Watcher] = {}
        self._by_identity: Dict[str, str] = {}
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._sequence = 0
        self._lock = RLock()

    def watch(self, identity_key: str, callback: Callable[[Dict[str, Any]], None], *, debounce_seconds: float = 0.05) -> str:
        if debounce_seconds < 0:
            raise ValueError("debounce_seconds must be non-negative")
        self.registry.identity(identity_key)
        with self._lock:
            existing = self._by_identity.get(identity_key)
            if existing:
                return existing
            if len(self._watchers) >= self.max_watchers:
                raise WatcherQuotaError("max_watchers exceeded")
            self._sequence += 1
            token = "watch-" + str(self._sequence)
            self._watchers[token] = _Watcher(token, identity_key, callback, float(debounce_seconds))
            self._by_identity[identity_key] = token
            return token

    def unwatch(self, token: str) -> bool:
        with self._lock:
            watcher = self._watchers.pop(str(token), None)
            if watcher is None:
                return False
            self._by_identity.pop(watcher.identity_key, None)
            self._pending.pop(watcher.identity_key, None)
            return True

    def emit(self, identity_key: str, paths: Iterable[str], *, trace_id: Optional[str] = None) -> None:
        self.registry.identity(identity_key)
        path_set = {str(path) for path in paths if str(path)}
        if not path_set:
            return
        with self._lock:
            if identity_key not in self._by_identity:
                raise WatcherError("identity has no watcher")
            event = self._pending.get(identity_key)
            if event is None:
                if len(self._pending) >= self.max_pending:
                    raise WatcherBackpressureError("max_pending exceeded")
                event = {
                    "identity_key": identity_key,
                    "paths": set(),
                    "first_seen": time.monotonic(),
                    "trace_id": str(trace_id or ""),
                }
                self._pending[identity_key] = event
            event["paths"].update(path_set)
            if trace_id:
                event["trace_id"] = str(trace_id)

    def flush(self, *, force: bool = False, now: Optional[float] = None) -> List[Dict[str, Any]]:
        current = time.monotonic() if now is None else float(now)
        callbacks = []
        with self._lock:
            for identity_key, pending in list(self._pending.items()):
                watcher = self._watchers.get(self._by_identity.get(identity_key, ""))
                if watcher is None:
                    self._pending.pop(identity_key, None)
                    continue
                if not force and current - pending["first_seen"] < watcher.debounce_seconds:
                    continue
                self._pending.pop(identity_key, None)
                self._sequence += 1
                callbacks.append((watcher.callback, {
                    "schema": "simplicio.map-service/v1",
                    "event": "watch_update",
                    "identity_key": identity_key,
                    "paths": sorted(pending["paths"]),
                    "trace_id": pending["trace_id"] or "watch-" + str(self._sequence),
                    "sequence": self._sequence,
                }))
        events = [event for _, event in callbacks]
        for callback, event in callbacks:
            if self.store is not None:
                self.store.invalidate(event["identity_key"], reason="watch_update")
            else:
                self.registry.invalidate(event["identity_key"], reason="watch_update")
            callback(dict(event))
        return events

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "schema": "simplicio.map-watcher-status/v1",
                "watchers": len(self._watchers),
                "pending": len(self._pending),
                "max_watchers": self.max_watchers,
                "max_pending": self.max_pending,
                "standalone": self.store is None,
                "identities": sorted(self._by_identity),
            }

    def verify(self) -> Dict[str, Any]:
        status = self.status()
        status["healthy"] = status["watchers"] <= status["max_watchers"] and status["pending"] <= status["max_pending"]
        return status

    def gc(self) -> List[str]:
        return self.store.gc() if self.store is not None else self.registry.gc()

    def rebind(self, old_identity_key: str, new_identity: Any) -> str:
        """Move a watcher onto a new identity after a branch/rebase transition.

        The old identity's snapshots are invalidated (never hard-removed here)
        so an in-use handle keeps working until its owner releases it; `gc()`
        reclaims the old snapshot once unreferenced, same as any invalidation.
        """
        with self._lock:
            token = self._by_identity.get(old_identity_key)
            watcher = self._watchers.get(token) if token else None
            new_key = self.registry.register(new_identity, transition=True)
            if watcher is None:
                return new_key
            pending = self._pending.pop(old_identity_key, None)
            self._by_identity.pop(old_identity_key, None)
            watcher.identity_key = new_key
            self._by_identity[new_key] = watcher.token
            if pending is not None:
                pending["identity_key"] = new_key
                self._pending[new_key] = pending
            return new_key

    def close(self) -> None:
        with self._lock:
            self._watchers.clear()
            self._by_identity.clear()
            self._pending.clear()
