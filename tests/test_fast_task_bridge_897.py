from __future__ import annotations

from pathlib import Path

import pytest

from simplicio_loop.fast_integration import FAST_CHANGESET_SCHEMA, FastStaleChangeset
from simplicio_loop.fast_task_bridge import FastTaskBridge, FastTaskBridgeError


class FakeStore:
    def __init__(self, root: Path, storage: Path | None) -> None:
        self.builds = 0
        self.pins: list[tuple[str, str]] = []
        self.overlays: list[tuple[str, str]] = []
        self.refreshes: list[tuple[str, str, str]] = []
        self.released: list[str] = []

    def build_base(self, *, config=None):
        self.builds += 1
        return {"generation_id": "base-1"}

    def pin(self, generation, owner, ttl_seconds=3600):
        self.pins.append((generation, owner))
        return {"lease_id": f"lease-{len(self.pins)}"}

    def create_overlay(self, worktree_id, base_generation):
        self.overlays.append((worktree_id, base_generation))
        return {"overlay_generation": f"overlay-{worktree_id}"}

    def refresh(self, worktree_id, base_generation, overlay_generation=None):
        self.refreshes.append((worktree_id, base_generation, overlay_generation or ""))
        return {"overlay_generation": f"{overlay_generation}-r1"}

    def release_lease(self, lease_id):
        self.released.append(lease_id)


def test_prepare_reuses_base_and_isolates_overlays(tmp_path: Path) -> None:
    store = FakeStore(tmp_path, None)
    bridge = FastTaskBridge(tmp_path, store_factory=lambda root, storage: store)
    mapper = {"generation": "mapper-1", "context_hash": "sha256:ctx"}
    first = bridge.prepare(task_id="t1", attempt_id="a1", worktree_id="w1", mapper_receipt=mapper)
    second = bridge.prepare(task_id="t2", attempt_id="a2", worktree_id="w2", mapper_receipt=mapper)
    assert store.builds == 1
    assert first.base_generation == second.base_generation == "base-1"
    assert first.overlay_generation != second.overlay_generation
    assert [item[0] for item in store.overlays] == ["w1", "w2"]
    bridge.release(first)
    assert store.released == ["lease-1"]


def test_missing_mapper_binding_fails_closed(tmp_path: Path) -> None:
    bridge = FastTaskBridge(tmp_path, store_factory=lambda root, storage: FakeStore(root, storage))
    with pytest.raises(FastTaskBridgeError, match="generation/context"):
        bridge.prepare(task_id="t", attempt_id="a", worktree_id="w", mapper_receipt={})


def test_refresh_is_affected_paths_only_and_stale_changeset_rejected(tmp_path: Path) -> None:
    store = FakeStore(tmp_path, None)
    bridge = FastTaskBridge(tmp_path, store_factory=lambda root, storage: store)
    binding = bridge.prepare(task_id="t", attempt_id="a", worktree_id="w", mapper_receipt={
        "generation": "mapper-1", "context_hash": "ctx",
    })
    assert bridge.refresh(binding, []) == binding
    refreshed = bridge.refresh(binding, ["b.py", "a.py", "a.py"])
    assert store.refreshes == [("w", "base-1", "overlay-w")]
    assert refreshed.overlay_generation == "overlay-w-r1"
    assert refreshed.to_dict()["refreshed_paths"] == ["a.py", "b.py"]
    candidate = {"schema": FAST_CHANGESET_SCHEMA, "changes": [{"path": "a.py"}],
                 "generation": "wrong", "context_hash": "ctx"}
    with pytest.raises(FastStaleChangeset):
        bridge.validate_changeset(binding, candidate)
