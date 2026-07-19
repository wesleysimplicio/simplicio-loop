import tempfile
from pathlib import Path
from typing import Optional

import pytest

from simplicio_loop.map_service import (
    AmbiguousRepositoryError,
    MapServiceError,
    MapServiceRegistry,
    RepositoryIdentity,
    UnknownRepositoryError,
)


def _identity(
    root: Path,
    *,
    worktree: Optional[Path] = None,
    repo: str = "owner/project",
) -> RepositoryIdentity:
    return RepositoryIdentity(
        repository=repo,
        canonical_root=str(root),
        worktree_root=str(worktree) if worktree else None,
        base_sha="abc123",
    )


def test_protocol_is_versioned_and_lists_full_contract() -> None:
    protocol = MapServiceRegistry.protocol()
    assert protocol["schema"] == "simplicio.map-service/v1"
    assert protocol["version"] == 1
    assert set(protocol["operations"]) == {
        "resolve_repo", "get_view", "build_canonical", "build_overlay",
        "subscribe", "invalidate", "release", "gc",
    }


def test_identity_resolves_canonical_and_more_specific_worktree() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        worktree = root / "worktree"
        worktree.mkdir()
        registry = MapServiceRegistry()
        canonical = _identity(root)
        overlay_identity = _identity(root, worktree=worktree)
        registry.register(canonical)
        registry.register(overlay_identity)
        assert registry.resolve_repo(str(root / "src")).key == canonical.key
        assert registry.resolve_repo(str(worktree / "src")).key == overlay_identity.key


def test_same_root_identity_collision_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        registry = MapServiceRegistry()
        registry.register(_identity(root))
        with pytest.raises(AmbiguousRepositoryError):
            registry.register(_identity(root, repo="other/project"))


def test_transition_supersedes_same_root_identity_and_invalidates_old_views() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        registry = MapServiceRegistry()
        old_key = registry.register(_identity(root))
        old_view = registry.build_canonical(old_key, tree_hash="sha1-tree")
        held = registry.get_view(old_view.cache_key)

        new_identity = RepositoryIdentity(
            repository="owner/project", canonical_root=str(root), base_sha="def456"
        )
        new_key = registry.register(new_identity, transition=True)

        assert new_key != old_key
        with pytest.raises(UnknownRepositoryError):
            registry.identity(old_key)
        assert registry.resolve_repo(str(root)).key == new_key

        assert registry.gc() == []
        registry.release(held.cache_key)
        assert registry.gc() == [old_view.cache_key]

        new_view = registry.build_canonical(new_key, tree_hash="sha2-tree")
        assert new_view.identity_key == new_key
        assert new_view.valid


def test_identity_requires_repository_and_base_sha() -> None:
    with tempfile.TemporaryDirectory() as directory:
        with pytest.raises(MapServiceError):
            RepositoryIdentity("", directory, base_sha="sha")
        with pytest.raises(MapServiceError):
            RepositoryIdentity("owner/project", directory)


def test_identity_key_includes_dirty_fingerprint_and_mapper_config() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        clean = RepositoryIdentity("owner/project", str(root), base_sha="sha")
        dirty = RepositoryIdentity(
            "owner/project", str(root), base_sha="sha", dirty=True,
            dirty_fingerprint="git-status-sha",
        )
        configured = RepositoryIdentity(
            "owner/project", str(root), base_sha="sha",
            mapper_config={"schema": "v1", "depth": 3},
        )
        assert clean.key != dirty.key
        assert clean.key != configured.key
        assert dirty.to_dict()["dirty_fingerprint"] == "git-status-sha"
        assert configured.to_dict()["mapper_config"]["depth"] == 3


def test_canonical_and_overlay_views_have_distinct_cache_keys() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        worktree = root / "wt"
        worktree.mkdir()
        registry = MapServiceRegistry()
        key = registry.register(_identity(root, worktree=worktree))
        canonical = registry.build_canonical(key, tree_hash="canonical-sha", files=[str(root / "src")])
        overlay = registry.build_overlay(key, tree_hash="overlay-sha", dirty_files=[str(worktree / "dirty.py")])
        assert canonical.mode == "canonical"
        assert overlay.mode == "overlay"
        assert canonical.cache_key != overlay.cache_key
        assert overlay.dirty
        assert registry.get_view(canonical.cache_key).references == 1
        assert registry.get_view(overlay.cache_key).trace_id


def test_overlay_without_worktree_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as directory:
        registry = MapServiceRegistry()
        key = registry.register(_identity(Path(directory)))
        with pytest.raises(MapServiceError):
            registry.build_overlay(key, tree_hash="sha")


def test_invalidate_notifies_subscribers_and_gc_respects_references() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        registry = MapServiceRegistry()
        key = registry.register(_identity(root))
        view = registry.build_canonical(key, tree_hash="sha")
        registry.get_view(view.cache_key)
        events = []
        registry.subscribe(key, events.append)
        assert registry.invalidate(key, reason="rebase") == [view.cache_key]
        assert events[0]["reason"] == "rebase"
        assert view.valid is False
        assert registry.gc() == []
        registry.release(view.cache_key)
        assert registry.gc() == [view.cache_key]
        with pytest.raises(UnknownRepositoryError):
            registry.get_view(view.cache_key)


def test_release_all_and_unknown_operations_are_safe() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        registry = MapServiceRegistry()
        key = registry.register(_identity(root))
        first = registry.build_canonical(key, tree_hash="one")
        second = registry.build_canonical(key, tree_hash="two")
        registry.get_view(first.cache_key)
        registry.get_view(second.cache_key)
        assert registry.release_all(key) == 2
        assert registry.gc() == []
        with pytest.raises(UnknownRepositoryError):
            registry.get_view("missing")
