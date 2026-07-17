"""Async single-flight map builds layered over MapServiceRegistry."""

import asyncio
from dataclasses import dataclass
from threading import RLock
from typing import Awaitable, Callable, Dict, Iterable, Tuple

from .map_service import MapServiceRegistry, MapView


class SingleFlightError(RuntimeError):
    """Raised when a single-flight build contract is invalid."""


@dataclass
class MapHandle:
    """Reference-counted client handle for one immutable map view."""

    view: MapView
    _store: "SingleFlightMapStore"
    _released: bool = False

    @property
    def cache_key(self) -> str:
        return self.view.cache_key

    @property
    def trace_id(self) -> str:
        return self.view.trace_id

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._store.registry.release(self.view.cache_key)

    def __enter__(self) -> "MapHandle":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


Builder = Callable[[], Awaitable[MapView]]


class SingleFlightMapStore:
    """Ensure at most one async owner builds a content-addressed view per key."""

    def __init__(self, registry: MapServiceRegistry) -> None:
        self.registry = registry
        self._inflight: Dict[Tuple[str, str, str, Tuple[str, ...]], asyncio.Future] = {}
        self._completed: Dict[Tuple[str, str, str, Tuple[str, ...]], str] = {}
        self._lock = RLock()
        self._owners = 0

    @staticmethod
    def _key(identity_key: str, mode: str, tree_hash: str, files: Iterable[str]):
        return (
            str(identity_key), str(mode), str(tree_hash),
            tuple(sorted(str(path) for path in files)),
        )

    @property
    def active_builds(self) -> int:
        with self._lock:
            return len(self._inflight)

    @property
    def owners_started(self) -> int:
        with self._lock:
            return self._owners

    async def get_or_build(
        self,
        identity_key: str,
        *,
        mode: str,
        tree_hash: str,
        files: Iterable[str] = (),
        builder: Builder,
    ) -> MapHandle:
        if mode not in ("canonical", "overlay"):
            raise SingleFlightError("mode must be canonical or overlay")
        file_tuple = tuple(sorted(str(path) for path in files))
        key = self._key(identity_key, mode, tree_hash, file_tuple)
        loop = asyncio.get_running_loop()
        owner = False
        with self._lock:
            cached_key = self._completed.get(key)
            if cached_key:
                try:
                    return MapHandle(self.registry.get_view(cached_key), self)
                except Exception:
                    self._completed.pop(key, None)
            future = self._inflight.get(key)
            if future is None:
                future = loop.create_future()
                self._inflight[key] = future
                self._owners += 1
                owner = True

        if owner:
            try:
                view = await builder()
                if not isinstance(view, MapView):
                    raise SingleFlightError("builder must return MapView")
                if view.identity_key != str(identity_key) or view.mode != mode:
                    raise SingleFlightError("builder returned a view for another key")
                with self._lock:
                    self._completed[key] = view.cache_key
                    self._inflight.pop(key, None)
                    if not future.done():
                        future.set_result(view.cache_key)
            except BaseException as exc:
                with self._lock:
                    self._inflight.pop(key, None)
                    if not future.done():
                        future.set_exception(exc)
                try:
                    await future
                except BaseException:
                    raise
        cache_key = await future
        return MapHandle(self.registry.get_view(cache_key), self)

    def invalidate(self, identity_key: str, *, reason: str = "source_changed"):
        invalidated = self.registry.invalidate(identity_key, reason=reason)
        invalidated_set = set(invalidated)
        with self._lock:
            for key, cache_key in list(self._completed.items()):
                if cache_key in invalidated_set:
                    self._completed.pop(key, None)
        return invalidated

    def gc(self):
        return self.registry.gc()
