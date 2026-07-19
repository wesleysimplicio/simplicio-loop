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


def test_multi_thread_multi_event_loop_access_is_a_documented_constraint() -> None:
    """A single SingleFlightMapStore/MapServiceRegistry instance is only safe to use
    WITHIN one asyncio event loop - `self._inflight` stores real `asyncio.Future`
    objects, which are bound to the loop that created them. A second OS thread running
    its OWN event loop (asyncio.run in a fresh thread) against the SAME instance hits a
    real asyncio cross-loop error, not a hang or silent corruption. This is the actual,
    verified concurrency contract (many concurrent TASKS in one loop - see the stress
    test below - not multiple threads each with their own loop), documented here as a
    real regression test rather than tribal knowledge."""
    import threading

    registry = MapServiceRegistry()
    identity_key = registry.register(RepositoryIdentity("owner/project", "/tmp", base_sha="sha"))
    store = SingleFlightMapStore(registry)
    errors = []

    def worker() -> None:
        async def builder():
            await asyncio.sleep(0.01)
            return registry.build_canonical(identity_key, tree_hash="tree")

        async def go():
            return await store.get_or_build(identity_key, mode="canonical", tree_hash="tree", builder=builder)

        try:
            asyncio.run(go())
        except RuntimeError as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert errors, (
        "expected a real asyncio cross-loop RuntimeError from concurrent multi-thread "
        "access - if this starts passing silently instead, the concurrency contract "
        "changed and this test (and its documentation) needs updating, not deleting"
    )


def test_heavy_concurrent_load_across_many_keys_within_one_event_loop() -> None:
    """The ACTUAL supported stress scenario (see the constraint test above): many
    concurrent tasks - not threads - sharing one event loop, across MULTIPLE distinct
    identities/tree_hashes, with invalidate()/gc() firing concurrently mid-flight. 200
    tasks across 10 real distinct keys, real per-key single-flight dedup verified."""
    async def scenario() -> None:
        registry = MapServiceRegistry()
        identities = [
            registry.register(RepositoryIdentity("owner/project-%d" % i, "/tmp/p-%d" % i, base_sha="s%d" % i))
            for i in range(10)
        ]
        store = SingleFlightMapStore(registry)
        build_counts = {key: 0 for key in identities}
        lock = asyncio.Lock()

        async def builder_for(key: str):
            async def builder():
                async with lock:
                    build_counts[key] += 1
                await asyncio.sleep(0.001)
                return registry.build_canonical(key, tree_hash="tree")
            return builder

        async def client(i: int):
            key = identities[i % len(identities)]
            handle = await store.get_or_build(
                key, mode="canonical", tree_hash="tree", builder=await builder_for(key),
            )
            # Interleave real invalidate/gc traffic on a DIFFERENT key than the one
            # this client just built, mid-flight relative to the other 199 tasks.
            other_key = identities[(i + 1) % len(identities)]
            if i % 37 == 0:
                store.invalidate(other_key, reason="stress-interleave")
                store.gc()
            return handle

        tasks = [asyncio.create_task(client(i)) for i in range(200)]
        handles = await asyncio.gather(*tasks)

        assert len(handles) == 200
        # Every key was built AT MOST once per still-valid generation - never once per
        # the 20 clients that shared it, proving dedup held under real heavy load.
        assert all(count <= 3 for count in build_counts.values()), (
            "a key's build count should reflect at most a few invalidate-triggered "
            "regenerations, never one per client (%r)" % build_counts
        )
        for handle in handles:
            handle.release()

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


def test_second_call_reuses_completed_build_without_invoking_builder() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = MapServiceRegistry()
            key = registry.register(
                RepositoryIdentity("owner/project", directory, base_sha="sha")
            )
            store = SingleFlightMapStore(registry)
            calls = 0

            async def builder():
                nonlocal calls
                calls += 1
                return registry.build_canonical(key, tree_hash="tree")

            first = await store.get_or_build(
                key, mode="canonical", tree_hash="tree", builder=builder
            )
            assert calls == 1
            assert store.owners_started == 1

            async def should_not_run():
                raise AssertionError("builder must not run on a cache hit")

            second = await store.get_or_build(
                key, mode="canonical", tree_hash="tree", builder=should_not_run
            )
            assert calls == 1
            assert store.owners_started == 1
            assert second.cache_key == first.cache_key
            first.release()
            second.release()

    asyncio.run(scenario())


def test_builder_returning_non_view_is_rejected() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = MapServiceRegistry()
            key = registry.register(
                RepositoryIdentity("owner/project", directory, base_sha="sha")
            )
            store = SingleFlightMapStore(registry)

            async def not_a_view():
                return {"not": "a MapView"}

            with pytest.raises(SingleFlightError):
                await store.get_or_build(
                    key, mode="canonical", tree_hash="tree", builder=not_a_view
                )
            assert store.active_builds == 0

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
