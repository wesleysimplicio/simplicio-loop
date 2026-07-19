import asyncio
import tempfile
from pathlib import Path

from simplicio_loop import map_service_persistence as persistence
from simplicio_loop.map_service import MapServiceRegistry, RepositoryIdentity
from simplicio_loop.map_service_single_flight import SingleFlightMapStore


def test_pinned_view_survives_gc_while_referenced() -> None:
    registry = MapServiceRegistry()
    with tempfile.TemporaryDirectory() as directory:
        key = registry.register(RepositoryIdentity("owner/project", directory, base_sha="sha"))
        view = registry.build_canonical(key, tree_hash="tree")
        registry.get_view(view.cache_key)
        assert view.references == 1
        registry.invalidate(key)
        assert registry.gc() == []
        registry.release(view.cache_key)
        assert registry.gc() == [view.cache_key]


def test_snapshot_and_restore_preserve_identities_views_and_leases() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = MapServiceRegistry()
            identity_key = registry.register(
                RepositoryIdentity("owner/project", str(root), base_sha="sha")
            )
            store = SingleFlightMapStore(registry)
            calls = 0

            async def builder():
                nonlocal calls
                calls += 1
                return registry.build_canonical(identity_key, tree_hash="tree")

            handle = await store.get_or_build(
                identity_key, mode="canonical", tree_hash="tree", builder=builder,
            )
            assert calls == 1
            pinned_cache_key = handle.cache_key
            assert registry.get_view(pinned_cache_key, acquire=False).references == 1

            snapshot_path = root / "map-service-snapshot.json"
            persistence.save(snapshot_path, registry, store)

            new_registry, new_store = persistence.load(snapshot_path)

            restored_identity = new_registry.identity(identity_key)
            assert restored_identity.repository == "owner/project"

            restored_view = new_registry.get_view(pinned_cache_key, acquire=False)
            assert restored_view.references == 1

            async def should_not_run():
                raise AssertionError("completed build must be recoverable without rebuilding")

            reused = await new_store.get_or_build(
                identity_key, mode="canonical", tree_hash="tree", builder=should_not_run,
            )
            assert reused.cache_key == pinned_cache_key
            assert new_store.owners_started == 0
            reused.release()

            new_registry.invalidate(identity_key)
            assert new_registry.gc() == []

            new_registry.release(pinned_cache_key)
            assert new_registry.gc() == [pinned_cache_key]

    asyncio.run(scenario())


def test_load_rejects_non_snapshot_payload(tmp_path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema": "not-a-snapshot"}', encoding="utf-8")
    try:
        persistence.load(bad)
        raised = False
    except ValueError:
        raised = True
    assert raised
