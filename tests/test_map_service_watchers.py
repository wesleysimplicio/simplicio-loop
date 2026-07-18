import tempfile
from pathlib import Path

import pytest

from simplicio_loop.map_service import MapServiceRegistry, RepositoryIdentity
from simplicio_loop.map_service_watchers import (
    MapWatcherManager,
    WatcherBackpressureError,
    WatcherError,
    WatcherQuotaError,
)


def _registry():
    directory = tempfile.TemporaryDirectory()
    registry = MapServiceRegistry()
    key = registry.register(RepositoryIdentity("owner/project", directory.name, base_sha="sha"))
    return directory, registry, key


def test_one_watcher_coalesces_paths_and_invalidates_view() -> None:
    directory, registry, key = _registry()
    try:
        view = registry.build_canonical(key, tree_hash="tree")
        events = []
        manager = MapWatcherManager(registry)
        token = manager.watch(key, events.append, debounce_seconds=1)
        assert manager.watch(key, events.append) == token
        manager.emit(key, ["a.py"], trace_id="trace-1")
        manager.emit(key, ["b.py", "a.py"])
        assert manager.status()["pending"] == 1
        assert manager.flush(force=True)[0]["paths"] == ["a.py", "b.py"]
        assert events[0]["trace_id"] == "trace-1"
        assert not view.valid
    finally:
        directory.cleanup()


def test_watcher_and_pending_quotas_fail_closed() -> None:
    first, registry, first_key = _registry()
    second = tempfile.TemporaryDirectory()
    try:
        second_key = registry.register(RepositoryIdentity("owner/other", second.name, base_sha="sha"))
        quota_manager = MapWatcherManager(registry, max_watchers=1, max_pending=1)
        quota_manager.watch(first_key, lambda event: None)
        with pytest.raises(WatcherQuotaError):
            quota_manager.watch(second_key, lambda event: None)

        pending_manager = MapWatcherManager(registry, max_watchers=2, max_pending=1)
        pending_manager.watch(first_key, lambda event: None)
        pending_manager.watch(second_key, lambda event: None)
        pending_manager.emit(first_key, ["one"])
        with pytest.raises(WatcherBackpressureError):
            pending_manager.emit(second_key, ["two"])
    finally:
        first.cleanup()
        second.cleanup()


def test_status_verify_standalone_restart_and_invalid_calls() -> None:
    directory, registry, key = _registry()
    try:
        manager = MapWatcherManager(registry, max_watchers=2, max_pending=2)
        assert manager.verify()["healthy"]
        with pytest.raises(WatcherError):
            manager.emit(key, ["missing"])
        token = manager.watch(key, lambda event: None)
        assert manager.unwatch(token)
        assert not manager.unwatch(token)
        manager.close()
        assert manager.status()["watchers"] == 0
        restarted = MapWatcherManager(registry)
        assert restarted.status()["standalone"]
        assert restarted.verify()["healthy"]
    finally:
        directory.cleanup()


def test_central_watcher_preserves_in_use_snapshot_until_handle_release() -> None:
    directory, registry, key = _registry()
    try:
        from simplicio_loop.map_service_single_flight import SingleFlightMapStore

        store = SingleFlightMapStore(registry)
        manager = MapWatcherManager(registry, store)

        async def build():
            return registry.build_canonical(key, tree_hash="tree")

        import asyncio
        handle = asyncio.run(store.get_or_build(key, mode="canonical", tree_hash="tree", builder=build))
        assert manager.watch(key, lambda event: None) == manager.watch(key, lambda event: None)
        manager.emit(key, ["dirty.py"])
        manager.flush(force=True)
        assert store.gc() == []
        handle.release()
        assert store.gc() == [handle.cache_key]
    finally:
        directory.cleanup()
