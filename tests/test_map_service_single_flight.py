import asyncio
import tempfile
from pathlib import Path

import pytest

from simplicio_loop.map_service import MapServiceRegistry, RepositoryIdentity
from simplicio_loop.map_service_single_flight import SingleFlightError, SingleFlightMapStore


def test_concurrent_builders_share_one_owner_and_snapshot() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = MapServiceRegistry()
            identity_key = registry.register(
                RepositoryIdentity("owner/project", str(root), base_sha="sha")
            )
            store = SingleFlightMapStore(registry)
            started = asyncio.Event()
            release = asyncio.Event()
            owners = 0

            async def builder():
                nonlocal owners
                owners += 1
                started.set()
                await release.wait()
                return registry.build_canonical(identity_key, tree_hash="tree")

            tasks = [
                asyncio.create_task(
                    store.get_or_build(
                        identity_key,
                        mode="canonical",
                        tree_hash="tree",
                        builder=builder,
                    )
                )
                for _ in range(24)
            ]
            await asyncio.wait_for(started.wait(), timeout=0.5)
            assert store.active_builds == 1
            release.set()
            handles = await asyncio.gather(*tasks)
            assert owners == 1
            assert store.owners_started == 1
            assert len({handle.cache_key for handle in handles}) == 1
            assert handles[0].view.references == 24
            for handle in handles:
                handle.release()
            store.invalidate(identity_key)
            assert store.gc() == [handles[0].cache_key]

    asyncio.run(scenario())


def test_failed_owner_releases_key_and_allows_retry() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = MapServiceRegistry()
            key = registry.register(
                RepositoryIdentity("owner/project", directory, base_sha="sha")
            )
            store = SingleFlightMapStore(registry)
            calls = 0

            async def failing():
                nonlocal calls
                calls += 1
                await asyncio.sleep(0)
                raise OSError("builder failed")

            results = await asyncio.gather(
                *[
                    store.get_or_build(
                        key,
                        mode="canonical",
                        tree_hash="bad",
                        builder=failing,
                    )
                    for _ in range(3)
                ],
                return_exceptions=True,
            )
            assert calls == 1
            assert all(isinstance(result, OSError) for result in results)
            assert store.active_builds == 0

            async def succeeding():
                nonlocal calls
                calls += 1
                return registry.build_canonical(key, tree_hash="good")

            handle = await store.get_or_build(
                key,
                mode="canonical",
                tree_hash="good",
                builder=succeeding,
            )
            assert calls == 2
            handle.release()

    asyncio.run(scenario())


def test_key_modes_are_isolated_and_bad_builders_fail_closed() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = MapServiceRegistry()
            key = registry.register(
                RepositoryIdentity("owner/project", directory, base_sha="sha")
            )
            store = SingleFlightMapStore(registry)

            async def wrong_view():
                return registry.build_canonical(key, tree_hash="tree")

            with pytest.raises(SingleFlightError):
                await store.get_or_build(
                    key, mode="overlay", tree_hash="tree", builder=wrong_view
                )
            assert store.active_builds == 0

            with pytest.raises(SingleFlightError):
                await store.get_or_build(
                    key, mode="invalid", tree_hash="tree", builder=wrong_view
                )

    asyncio.run(scenario())
