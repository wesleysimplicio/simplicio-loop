import asyncio

from simplicio_loop.map_service import MapServiceRegistry, RepositoryIdentity
from simplicio_loop.map_service_leases import BuildLeaseTable
from simplicio_loop.map_service_single_flight import SingleFlightError, SingleFlightMapStore


def test_expired_owner_can_be_replaced_but_live_owner_cannot():
    table = BuildLeaseTable()
    first = table.acquire("same-build", ttl=10, now=100)
    assert first is not None
    assert table.acquire("same-build", ttl=10, now=105) is None
    replacement = table.acquire("same-build", ttl=10, now=111)
    assert replacement is not None
    assert replacement.token != first.token
    assert table.release(first) is False
    assert table.release(replacement) is True


def test_single_flight_lease_times_out_and_does_not_leave_owner_stuck():
    async def scenario():
        registry = MapServiceRegistry()
        identity = RepositoryIdentity(repository="r", canonical_root="/repo", base_sha="sha")
        key = registry.register(identity)
        store = SingleFlightMapStore(registry, lease_seconds=0.02)

        async def stuck():
            await asyncio.sleep(1)

        try:
            await store.get_or_build(key, mode="canonical", tree_hash="sha", builder=stuck)
        except (asyncio.TimeoutError, SingleFlightError):
            pass
        else:
            raise AssertionError("expired build lease did not fail")
        assert store.active_builds == 0
        assert store.leases.status()["active"] == 0

    asyncio.run(scenario())
