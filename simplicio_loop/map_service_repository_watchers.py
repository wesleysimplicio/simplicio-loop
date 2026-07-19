"""One debounced watcher coordination surface per repository.

This layer deliberately receives filesystem/Git events from an adapter instead of starting
one OS watcher per client. Clients subscribe to identities, while the repository key owns
coalescing, ordering, and transition detection.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Callable, Dict, Iterable, List, Optional, Set


@dataclass
class _Subscription:
    token: str
    identity_key: str
    callback: Callable[[Dict[str, Any]], None]


@dataclass
class _Repository:
    subscriptions: Dict[str, _Subscription] = field(default_factory=dict)
    pending: Optional[Dict[str, Any]] = None
    last_transition: Optional[tuple[str, str]] = None


class RepositoryWatcherHub:
    """Coalesce all client events behind one logical watcher per repository."""

    def __init__(self, *, debounce_seconds: float = 0.05, max_repositories: int = 64) -> None:
        if debounce_seconds < 0 or max_repositories < 1:
            raise ValueError("watcher limits must be non-negative/positive")
        self.debounce_seconds = float(debounce_seconds)
        self.max_repositories = int(max_repositories)
        self._repositories: Dict[str, _Repository] = {}
        self._sequence = 0
        self._lock = RLock()

    def subscribe(self, repository_key: str, identity_key: str, callback: Callable[[Dict[str, Any]], None]) -> str:
        repository_key, identity_key = str(repository_key), str(identity_key)
        with self._lock:
            repository = self._repositories.get(repository_key)
            if repository is None:
                if len(self._repositories) >= self.max_repositories:
                    raise RuntimeError("max_repositories exceeded")
                repository = self._repositories.setdefault(repository_key, _Repository())
            self._sequence += 1
            token = "repo-watch-%d" % self._sequence
            repository.subscriptions[token] = _Subscription(token, identity_key, callback)
            return token

    def unsubscribe(self, token: str) -> bool:
        with self._lock:
            for repository_key, repository in list(self._repositories.items()):
                if token in repository.subscriptions:
                    del repository.subscriptions[token]
                    if not repository.subscriptions and repository.pending is None:
                        del self._repositories[repository_key]
                    return True
        return False

    def emit(self, repository_key: str, identity_key: str, paths: Iterable[str], *, active: bool = False, reason: str = "source_changed") -> None:
        path_set = {str(path) for path in paths if str(path)}
        if not path_set:
            return
        with self._lock:
            repository = self._repositories.get(str(repository_key))
            if repository is None:
                raise RuntimeError("repository has no watcher")
            pending = repository.pending
            if pending is None:
                pending = repository.pending = {
                    "repository_key": str(repository_key), "identity_keys": set(), "paths": set(),
                    "first_seen": time.monotonic(), "priority": 0 if active else 1,
                    "reason": str(reason),
                }
            pending["identity_keys"].add(str(identity_key))
            pending["paths"].update(path_set)
            pending["priority"] = min(pending["priority"], 0 if active else 1)
            if reason != "source_changed":
                pending["reason"] = str(reason)

    def observe_transition(self, repository_key: str, identity_key: str, *, head: str, branch: str) -> bool:
        """Detect branch switch/rebase/HEAD advance and enqueue one coalesced event."""
        with self._lock:
            repository = self._repositories.get(str(repository_key))
            if repository is None:
                raise RuntimeError("repository has no watcher")
            marker = (str(head), str(branch))
            changed = repository.last_transition is not None and repository.last_transition != marker
            repository.last_transition = marker
        if changed:
            self.emit(repository_key, identity_key, [".git/HEAD", ".git/index"], reason="branch_transition")
        return changed

    def flush(self, *, force: bool = False, now: Optional[float] = None) -> List[Dict[str, Any]]:
        current = time.monotonic() if now is None else float(now)
        callbacks: List[tuple[Callable[[Dict[str, Any]], None], Dict[str, Any]]] = []
        with self._lock:
            ready = []
            for key, repository in self._repositories.items():
                event = repository.pending
                if event is None or (not force and current - event["first_seen"] < self.debounce_seconds):
                    continue
                repository.pending = None
                self._sequence += 1
                ready.append((event["priority"], event["first_seen"], key, repository, event))
            for _priority, _first_seen, key, repository, event in sorted(ready, key=lambda item: (item[0], item[1])):
                payload = {
                    "schema": "simplicio.map-service-repository-watcher/v1",
                    "event": "repository_watch_update", "repository_key": key,
                    "identity_keys": sorted(event["identity_keys"]), "paths": sorted(event["paths"]),
                    "reason": event["reason"], "sequence": self._sequence,
                }
                callbacks.extend((subscription.callback, dict(payload)) for subscription in repository.subscriptions.values())
        for callback, payload in callbacks:
            callback(payload)
        return [payload for _callback, payload in callbacks]

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "schema": "simplicio.map-service-repository-watcher-status/v1",
                "repositories": len(self._repositories),
                "subscriptions": sum(len(repo.subscriptions) for repo in self._repositories.values()),
                "pending": sum(repo.pending is not None for repo in self._repositories.values()),
                "max_repositories": self.max_repositories,
            }
