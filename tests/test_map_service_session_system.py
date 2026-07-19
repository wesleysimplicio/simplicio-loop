"""Combined system test: registry + single-flight store + watchers wired through
MapServiceSession, restart-safety of its counters, and the `simplicio-loop map status` CLI."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from simplicio_loop import cli
from simplicio_loop.map_service import (
    MapServiceRegistry,
    RepositoryIdentity,
    UnknownRepositoryError,
)
from simplicio_loop.map_service_status import (
    SCHEMA,
    MapServiceSession,
    default_status_path,
    load_status_file,
)


def test_session_counts_hits_builds_waits_and_watcher_invalidations() -> None:
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = MapServiceSession()
            identity_key = session.registry.register(
                RepositoryIdentity("owner/project", str(root), base_sha="sha")
            )
            build_started = asyncio.Event()
            release = asyncio.Event()
            builder_calls = 0

            async def slow_builder():
                nonlocal builder_calls
                builder_calls += 1
                build_started.set()
                await release.wait()
                return session.registry.build_canonical(identity_key, tree_hash="tree-1")

            owner_task = asyncio.create_task(
                session.get_or_build(
                    identity_key, mode="canonical", tree_hash="tree-1", builder=slow_builder
                )
            )
            await build_started.wait()
            waiter_task = asyncio.create_task(
                session.get_or_build(
                    identity_key, mode="canonical", tree_hash="tree-1", builder=slow_builder
                )
            )
            await asyncio.sleep(0)
            release.set()
            owner_handle, waiter_handle = await asyncio.gather(owner_task, waiter_task)
            assert builder_calls == 1
            assert owner_handle.cache_key == waiter_handle.cache_key

            async def unused_builder():
                raise AssertionError("cache hit must not invoke the builder")

            hit_handle = await session.get_or_build(
                identity_key, mode="canonical", tree_hash="tree-1", builder=unused_builder
            )
            assert hit_handle.cache_key == owner_handle.cache_key

            token = session.watchers.watch(identity_key, lambda event: None, debounce_seconds=0.0)
            session.watchers.emit(identity_key, ["a.py"])
            flushed = session.watchers.flush(force=True)
            assert len(flushed) == 1
            session.watchers.unwatch(token)

            counters = session.counters()
            assert counters["builds"] == 1
            assert counters["waits"] == 1
            assert counters["cache_hits"] == 1
            assert counters["invalidations"] == 1

            status = session.status()
            assert status["schema"] == SCHEMA
            assert status["watchers"]["watchers"] == 0
            return session, root

    asyncio.run(scenario())


def test_rebind_through_session_supersedes_identity_and_moves_watcher() -> None:
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = MapServiceSession()
            old_key = session.registry.register(
                RepositoryIdentity(
                    "owner/project", str(root), base_sha="branch-a-sha"
                )
            )

            async def build_old():
                return session.registry.build_canonical(old_key, tree_hash="branch-a-tree")

            old_handle = await session.get_or_build(
                old_key, mode="canonical", tree_hash="branch-a-tree", builder=build_old
            )

            events: list = []
            session.watchers.watch(old_key, events.append)
            session.watchers.emit(old_key, ["pending.py"])

            new_identity = RepositoryIdentity(
                "owner/project", str(root), base_sha="branch-b-sha"
            )
            new_key = session.rebind(old_key, new_identity)

            assert new_key != old_key
            with pytest.raises(UnknownRepositoryError):
                session.registry.identity(old_key)
            assert session.watchers.status()["identities"] == [new_key]
            assert session.counters()["invalidations"] == 1

            # A build request against the superseded key can no longer succeed —
            # callers must move to the key `rebind` returned. The prior view is
            # invalidated (not merely cached), so the store re-invokes the builder
            # instead of serving a stale hit, and the builder itself now fails
            # closed because the identity is gone from the registry.
            async def build_against_stale_identity():
                return session.registry.build_canonical(old_key, tree_hash="branch-a-tree")

            with pytest.raises(UnknownRepositoryError):
                await session.get_or_build(
                    old_key, mode="canonical", tree_hash="branch-a-tree",
                    builder=build_against_stale_identity,
                )

            async def build_new():
                return session.registry.build_canonical(new_key, tree_hash="branch-b-tree")

            new_handle = await session.get_or_build(
                new_key, mode="canonical", tree_hash="branch-b-tree", builder=build_new
            )
            assert new_handle.view.identity_key == new_key
            assert new_handle.view.valid

            session.watchers.emit(new_key, ["after-switch.py"])
            flushed = session.watchers.flush(force=True)
            assert flushed[0]["identity_key"] == new_key
            assert set(flushed[0]["paths"]) == {"pending.py", "after-switch.py"}
            assert events[-1]["identity_key"] == new_key

            # The old snapshot survives until its handle is released, then GC reclaims it.
            assert session.gc() == []
            old_handle.release()
            assert session.gc() == [old_handle.cache_key]

    asyncio.run(scenario())


def test_status_file_survives_restart_and_cli_reports_it_without_fabrication() -> None:
    async def build_once(session, identity_key):
        async def builder():
            return session.registry.build_canonical(identity_key, tree_hash="tree-x")

        return await session.get_or_build(
            identity_key, mode="canonical", tree_hash="tree-x", builder=builder
        )

    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        session = MapServiceSession()
        identity_key = session.registry.register(
            RepositoryIdentity("owner/project", str(repo), base_sha="sha")
        )
        asyncio.run(build_once(session, identity_key))
        session.invalidate(identity_key, reason="restart-safety-drill")
        status_path = default_status_path(str(repo))
        session.write_status_file(status_path)

        del session

        reloaded = load_status_file(status_path)
        assert reloaded["counters"]["builds"] == 1
        assert reloaded["counters"]["invalidations"] == 1

        exit_code = cli.main(["map", "status", "--repo", str(repo), "--json"])
        assert exit_code == 0


def test_map_status_cli_fails_closed_when_no_session_has_run(capsys) -> None:
    with tempfile.TemporaryDirectory() as directory:
        exit_code = cli.main(["map", "status", "--repo", directory, "--json"])
        assert exit_code == 1
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "UNAVAILABLE"
        assert out["reason_code"] == "status_file_missing"
