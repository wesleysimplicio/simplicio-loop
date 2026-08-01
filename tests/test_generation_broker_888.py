from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from simplicio_loop.checkpoint_lifecycle import CheckpointLifecycle, LifecycleError
from simplicio_loop.fast_fanout import CanonicalGeneration
from simplicio_loop.generation_broker import GenerationBroker
from simplicio_loop.map_service import MapServiceRegistry, RepositoryIdentity


def broker(tmp_path: Path) -> tuple[GenerationBroker, str, CanonicalGeneration]:
    base = tmp_path / "base"
    base.mkdir()
    registry = MapServiceRegistry()
    identity = RepositoryIdentity(
        repository="owner/project", canonical_root=str(base), base_sha="abc"
    )
    identity_key = registry.register(identity)
    generation = CanonicalGeneration("generation-1", "ctx", "abc", "plan", "receipt")
    lifecycle = CheckpointLifecycle(
        tmp_path / "runs",
        task_id="task-888",
        attempt_id="attempt-1",
        source_commit="abc",
        fast_generation=generation.generation,
        base_path=base,
    )
    return GenerationBroker(registry, lifecycle), identity_key, generation


def test_candidates_share_canonical_cache_but_keep_isolated_overlays(tmp_path: Path):
    service, identity_key, generation = broker(tmp_path)
    first = service.bind(
        identity_key,
        tree_hash="tree",
        files=["b.py", "a.py"],
        candidate_id="a",
        generation=generation,
    )
    second = service.bind(
        identity_key,
        tree_hash="tree",
        files=["a.py", "b.py"],
        candidate_id="b",
        generation=generation,
    )

    assert first.canonical_cache_key == second.canonical_cache_key
    assert first.overlay_path != second.overlay_path
    assert first.receipt_hash != second.receipt_hash
    marker = json.loads((Path(first.overlay_path) / "overlay.json").read_text())
    assert marker["base_read_only"] is True
    assert first.to_dict()["generation"] == generation.to_dict()


def test_stale_generation_fails_before_overlay_creation(tmp_path: Path):
    service, identity_key, generation = broker(tmp_path)
    stale = CanonicalGeneration("generation-2", "ctx", "abc", "plan", "receipt")

    with pytest.raises(LifecycleError, match="stale canonical generation"):
        service.bind(
            identity_key,
            tree_hash="tree",
            files=[],
            candidate_id="stale",
            generation=stale,
        )

    assert not service.lifecycle.overlays.exists()
    assert generation.generation == service.lifecycle.fast_generation


def test_concurrent_bindings_share_generation_without_overlay_collisions(tmp_path: Path):
    service, identity_key, generation = broker(tmp_path)

    def bind(candidate_id: str):
        return service.bind(
            identity_key,
            tree_hash="tree",
            files=["a.py"],
            candidate_id=candidate_id,
            generation=generation,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        bindings = list(executor.map(bind, (f"candidate-{index}" for index in range(20))))

    assert len({item.canonical_cache_key for item in bindings}) == 1
    assert len({item.overlay_path for item in bindings}) == len(bindings)
    assert all(Path(item.overlay_path).is_dir() for item in bindings)


def test_gc_preserves_pinned_overlay_then_reclaims_cancelled_candidate(tmp_path: Path):
    service, identity_key, generation = broker(tmp_path)
    binding = service.bind(
        identity_key,
        tree_hash="tree",
        files=["a.py"],
        candidate_id="a",
        generation=generation,
    )
    service.lifecycle.checkpoint("a", "candidate", "CANCELLED")
    service.lifecycle.cancel(["a"], reason="lost")
    service.lifecycle.lease("a", expires_ns=200)

    assert service.gc(retention_ns=0, now_ns=100, apply=True)["removed"] == []
    assert service.gc(retention_ns=0, now_ns=300, apply=True)["removed"] == ["a"]
    assert not Path(binding.overlay_path).exists()
