from __future__ import annotations

from pathlib import Path

from simplicio_loop.fast_task_bridge import FastTaskBridge
from simplicio_loop.local_first_path import LocalFirstTaskPath


class Store:
    def __init__(self, root: Path, storage: Path | None) -> None:
        self.builds = 0

    def build_base(self, *, config=None):
        self.builds += 1
        return {"generation_id": "base"}

    def pin(self, generation, owner, ttl_seconds=3600):
        return {"lease_id": "lease"}

    def create_overlay(self, worktree_id, base_generation):
        return {"overlay_generation": "overlay"}

    def release_lease(self, lease_id):
        return None


class Integration:
    def prepare(self, task):
        return {"status": "READY", "generation": "overlay", "context_hash": "ctx"}

    def apply(self, changeset, *, winner, generation, context_hash):
        assert generation == "overlay"
        assert context_hash == "ctx"
        return {"status": "APPLIED", "changes": len(changeset["changes"])}


def test_local_first_path_binds_mapper_fast_and_apply(tmp_path: Path) -> None:
    store = Store(tmp_path, None)
    path = LocalFirstTaskPath(str(tmp_path),
                              bridge=FastTaskBridge(tmp_path, store_factory=lambda root, storage: store),
                              integration=Integration())
    result = path.run(task_id="task", attempt_id="attempt", worktree_id="worktree",
                      task="change app", mapper_receipt={"generation": "mapper", "context_hash": "ctx"},
                      changeset={"schema": "simplicio.fast.changeset/v2", "generation": "overlay",
                                 "context_hash": "ctx", "changes": [{"path": "app.py"}]})
    assert result.status == "APPLIED"
    assert result.binding.base_generation == "base"
    assert result.to_dict()["apply_receipt"]["status"] == "APPLIED"
    assert store.builds == 1
