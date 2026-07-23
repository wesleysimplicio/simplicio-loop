import pytest

from simplicio_loop.context_graph_fanout import (
    CanonicalMapClient,
    ConflictGraphError,
    TaskEnvelope,
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
