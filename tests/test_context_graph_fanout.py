import pytest
from concurrent.futures import ThreadPoolExecutor

from simplicio_loop.context_graph_fanout import (
    CanonicalMapClient,
    ConflictGraphError,
    TaskEnvelope,
    WorktreeMapLeaseManager,
    execution_waves,
)
from simplicio_loop.map_service import MapServiceRegistry, RepositoryIdentity


def test_disjoint_tasks_share_wave_and_mutation_conflicts_are_ordered():
    tasks = [
        TaskEnvelope("a", mutation_targets=("a.py",)),
        TaskEnvelope("b", mutation_targets=("b.py",)),
        TaskEnvelope("c", mutation_targets=("a.py",)),
    ]
    result = execution_waves(tasks, capacity=3)
    assert result["waves"] == [["a", "b"], ["c"]]
    assert result["graph"]["c"]["reasons"]["a"][0]["code"] == "shared_mutation_target"


def test_dependencies_and_cycles_fail_closed():
    result = execution_waves([TaskEnvelope("b", depends_on=("a",)), TaskEnvelope("a")])
    assert result["waves"] == [["a"], ["b"]]
    with pytest.raises(ConflictGraphError, match="cyclic"):
        execution_waves([TaskEnvelope("a", depends_on=("b",)), TaskEnvelope("b", depends_on=("a",))])


def test_map_client_reuses_canonical_handle_and_releases_reference(tmp_path):
    registry = MapServiceRegistry()
    identity = RepositoryIdentity("repo", str(tmp_path), base_sha="abc")
    key = registry.register(identity)
    client = CanonicalMapClient(registry)
    first = client.request_canonical(key, tree_hash="tree", files=(str(tmp_path / "a.py"),))
    second = client.request_canonical(key, tree_hash="tree", files=(str(tmp_path / "a.py"),))
    assert first.status == second.status == "ready"
    assert second.cache_hit is True
    assert registry.get_view(first.cache_key, acquire=False).references == 2
    client.release(second)
    assert registry.get_view(first.cache_key, acquire=False).references == 1


def test_map_client_reports_degraded_without_fabricating_cache_hit():
    handle = CanonicalMapClient().request_canonical("missing", tree_hash="tree")
    assert handle.status == "degraded"
    assert handle.fallback is True
    assert handle.cache_hit is False


def _registered_views(tmp_path):
    registry = MapServiceRegistry()
    canonical = RepositoryIdentity("repo", str(tmp_path), base_sha="abc", mapper_config={"v": 1})
    worktree = RepositoryIdentity("repo", str(tmp_path), worktree_root=str(tmp_path / "wt"),
                                  base_sha="abc", dirty=True, dirty_fingerprint="dirty",
                                  mapper_config={"v": 1})
    return registry, registry.register(canonical), registry.register(worktree)


def test_worktree_binding_requires_authority_and_binds_snapshot_overlay_config(tmp_path):
    registry, canonical_key, worktree_key = _registered_views(tmp_path)
    manager = WorktreeMapLeaseManager(CanonicalMapClient(registry))
    with pytest.raises(ConflictGraphError, match="authority"):
        manager.bind(TaskEnvelope("unsafe"), owner_id="worker", canonical_identity=canonical_key,
                     canonical_tree_hash="tree", canonical_files=("a.py",),
                     worktree_identity=worktree_key, overlay_tree_hash="dirty", dirty_files=("a.py",))
    binding = manager.bind(TaskEnvelope("safe", authority_hash="auth"), owner_id="worker",
                           canonical_identity=canonical_key, canonical_tree_hash="tree",
                           canonical_files=("a.py",), worktree_identity=worktree_key,
                           overlay_tree_hash="dirty", dirty_files=("a.py",))
    assert binding.canonical.mode == "canonical" and binding.overlay.mode == "overlay"
    assert binding.canonical.schema_identity == "simplicio.map-service/v1"
    assert binding.canonical.config_identity == binding.overlay.config_identity


def test_drift_replans_only_affected_task_and_crash_releases_without_early_gc(tmp_path):
    registry, canonical_key, worktree_key = _registered_views(tmp_path)
    manager = WorktreeMapLeaseManager(CanonicalMapClient(registry))
    kwargs = dict(canonical_identity=canonical_key, canonical_tree_hash="tree",
                  canonical_files=("a.py",), worktree_identity=worktree_key,
                  overlay_tree_hash="dirty", dirty_files=("a.py",))
    left = manager.bind(TaskEnvelope("left", authority_hash="a"), owner_id="crashed", **kwargs)
    right = manager.bind(TaskEnvelope("right", authority_hash="b"), owner_id="healthy", **kwargs)
    changed = manager.replan_drift("left", overlay_tree_hash="dirty-2", dirty_files=("b.py",))
    assert changed.generation == 2 and right.generation == 1
    registry.invalidate(worktree_key, reason="overlay_drift")
    removed = registry.gc()
    assert len(removed) == 1  # the released pre-drift overlay, never an active handle
    assert registry.get_view(left.canonical.cache_key, acquire=False).references == 2
    assert manager.recover_owner("crashed") == ("left",)
    assert manager.status()["tasks"] == ["right"]
    assert manager.release("right")
    assert manager.status()["active"] == 0


def test_binding_lifecycle_is_idempotent_and_duplicate_task_fails_closed(tmp_path):
    registry, canonical_key, worktree_key = _registered_views(tmp_path)
    manager = WorktreeMapLeaseManager(CanonicalMapClient(registry))
    kwargs = dict(owner_id="owner", canonical_identity=canonical_key, canonical_tree_hash="tree",
                  canonical_files=(), worktree_identity=worktree_key,
                  overlay_tree_hash="dirty", dirty_files=())
    manager.bind(TaskEnvelope("task", authority_hash="auth"), **kwargs)
    with pytest.raises(ConflictGraphError, match="already"):
        manager.bind(TaskEnvelope("task", authority_hash="auth"), **kwargs)
    assert manager.recover_owner("missing") == ()
    assert manager.release("task") is True
    assert manager.release("task") is False


def test_concurrent_worktrees_share_canonical_and_keep_distinct_overlays(tmp_path):
    registry = MapServiceRegistry()
    canonical_key = registry.register(RepositoryIdentity("repo", str(tmp_path), base_sha="abc"))
    worktrees = [
        registry.register(RepositoryIdentity("repo", str(tmp_path),
            worktree_root=str(tmp_path / ("wt-%d" % index)), base_sha="abc",
            dirty=True, dirty_fingerprint=str(index)))
        for index in range(8)
    ]
    manager = WorktreeMapLeaseManager(CanonicalMapClient(registry))
    def bind(index):
        return manager.bind(TaskEnvelope(str(index), authority_hash="auth"),
            owner_id="worker", canonical_identity=canonical_key, canonical_tree_hash="tree",
            canonical_files=("shared.py",), worktree_identity=worktrees[index],
            overlay_tree_hash=str(index), dirty_files=("f%d.py" % index,))
    with ThreadPoolExecutor(max_workers=8) as pool:
        bindings = list(pool.map(bind, range(8)))
    assert len({binding.canonical.cache_key for binding in bindings}) == 1
    assert len({binding.overlay.cache_key for binding in bindings}) == 8
    assert manager.status()["metrics"]["cache_hits"] == 7
    assert manager.recover_owner("worker") == tuple(str(index) for index in range(8))


def test_soft_conflicts_are_reasoned_and_invalid_inputs_fail_closed():
    result = execution_waves([
        TaskEnvelope("a", tests=("suite",), resources=("gpu",)),
        TaskEnvelope("b", tests=("suite",), resources=("gpu",)),
    ])
    reasons = result["graph"]["b"]["reasons"]["a"]
    assert {reason["code"] for reason in reasons} == {"test_contention", "resource_contention"}
    assert all(reason["hard"] is False for reason in reasons)
    with pytest.raises(ConflictGraphError, match="unknown dependency"):
        execution_waves([TaskEnvelope("a", depends_on=("missing",))])
    with pytest.raises(ValueError, match="capacity"):
        execution_waves([TaskEnvelope("a")], capacity=0)
