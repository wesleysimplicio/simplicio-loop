import tempfile
import time
from pathlib import Path

import pytest

from simplicio_loop.map_service import (
    MapServiceRegistry,
    RepositoryIdentity,
    UnknownRepositoryError as RegistryUnknownRepositoryError,
)
from simplicio_loop.map_service_single_flight import SingleFlightMapStore
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


def test_flush_without_force_respects_debounce_window() -> None:
    directory, registry, key = _registry()
    try:
        manager = MapWatcherManager(registry)
        events = []
        manager.watch(key, events.append, debounce_seconds=10)
        start = time.monotonic()
        manager.emit(key, ["a.py"])
        assert manager.flush(now=start) == []
        assert manager.status()["pending"] == 1
        assert events == []
        flushed = manager.flush(now=start + 20)
        assert flushed[0]["paths"] == ["a.py"]
        assert events[0]["paths"] == ["a.py"]
        assert manager.status()["pending"] == 0
    finally:
        directory.cleanup()


def test_manager_with_store_delegates_invalidate_and_gc() -> None:
    directory, registry, key = _registry()
    try:
        store = SingleFlightMapStore(registry)
        manager = MapWatcherManager(registry, store)
        view = registry.build_canonical(key, tree_hash="tree")
        events = []
        manager.watch(key, events.append)
        manager.emit(key, ["a.py"])
        manager.flush(force=True)
        assert not view.valid
        assert manager.gc() == [view.cache_key]
    finally:
        directory.cleanup()


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


def test_two_clients_share_one_watcher_and_gc_keeps_in_use_snapshot() -> None:
    directory, registry, key = _registry()
    try:
        store = SingleFlightMapStore(registry)
        manager = MapWatcherManager(registry, store)

        client_a_events: list = []
        client_b_events: list = []
        token_a = manager.watch(key, client_a_events.append, debounce_seconds=1)
        token_b = manager.watch(key, client_b_events.append, debounce_seconds=1)
        assert token_a == token_b
        assert manager.status()["watchers"] == 1

        async def build():
            return registry.build_canonical(key, tree_hash="tree")

        import asyncio

        handle = asyncio.run(
            store.get_or_build(key, mode="canonical", tree_hash="tree", builder=build)
        )
        manager.emit(key, ["shared.py"], trace_id="client-a")
        assert manager.flush(force=True)[0]["identity_key"] == key
        assert client_a_events and client_a_events[0]["trace_id"] == "client-a"
        assert client_b_events == []

        assert manager.gc() == []
        handle.release()
        assert manager.gc() == [handle.cache_key]
    finally:
        directory.cleanup()


def test_rebind_moves_watcher_to_new_identity_after_branch_switch() -> None:
    directory, registry, key = _registry()
    try:
        store = SingleFlightMapStore(registry)
        manager = MapWatcherManager(registry, store)

        async def build_old():
            return registry.build_canonical(key, tree_hash="branch-a-tree")

        import asyncio

        old_handle = asyncio.run(
            store.get_or_build(key, mode="canonical", tree_hash="branch-a-tree", builder=build_old)
        )
        events: list = []
        token = manager.watch(key, events.append)
        manager.emit(key, ["pending.py"])

        new_identity = RepositoryIdentity(
            repository="owner/project", canonical_root=directory.name, base_sha="branch-b-sha"
        )
        new_key = manager.rebind(key, new_identity)

        assert new_key != key
        with pytest.raises(RegistryUnknownRepositoryError):
            registry.identity(key)
        assert manager.status()["identities"] == [new_key]

        assert manager.gc() == []
        old_handle.release()
        assert manager.gc() == [old_handle.cache_key]

        async def build_new():
            return registry.build_canonical(new_key, tree_hash="branch-b-tree")

        new_handle = asyncio.run(
            store.get_or_build(new_key, mode="canonical", tree_hash="branch-b-tree", builder=build_new)
        )
        assert new_handle.view.identity_key == new_key
        assert new_handle.view.valid

        manager.emit(new_key, ["after-switch.py"])
        flushed = manager.flush(force=True)
        assert flushed[0]["identity_key"] == new_key
        assert set(flushed[0]["paths"]) == {"pending.py", "after-switch.py"}
        assert events[-1]["identity_key"] == new_key
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
