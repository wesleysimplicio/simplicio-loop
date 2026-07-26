from __future__ import annotations

import pytest

from simplicio_loop.fast_fanout import FastFanoutCoordinator, FastFanoutError
from simplicio_loop.fast_integration import FAST_CHANGESET_SCHEMA


class _FakeIntegration:
    def __init__(self):
        self.prepares = 0
        self.applies = []
        self.refreshes = 0

    def prepare(self, task):
        self.prepares += 1
        generation = f"g{self.prepares}"
        context = f"ctx{self.prepares}"
        return {"status": "READY", "generation": generation,
                "context_hash": context, "plan_hash": f"plan{self.prepares}"}

    def apply(self, changeset, *, winner, generation, context_hash):
        self.applies.append((winner, generation, context_hash))
        return {"status": "READY", "applied": winner}

    def refresh(self):
        self.refreshes += 1
        return {"status": "MEASURED", "generation": "g2"}

    def rollout(self, mode, *, generation=None, reason=None):
        return {"status": "rolled-back" if mode == "rollback" else "accepted",
                "mode": mode, "generation": generation, "reason": reason}


def _changeset(generation="g1", context_hash="ctx1"):
    return {"schema": FAST_CHANGESET_SCHEMA,
            "changes": [{"path": "app.py", "expected_sha256": "a" * 64,
                          "replacements": [{"start_line": 1, "end_line": 1, "content": "x"}]}],
            "generation": generation, "context_hash": context_hash}


def test_one_prepare_is_shared_and_only_verified_winner_promotes(tmp_path):
    fake = _FakeIntegration()
    coordinator = FastFanoutCoordinator(tmp_path, integration=fake)
    prepared = coordinator.prepare("change app")
    assert prepared["status"] == "MEASURED"
    first = coordinator.prepare("change app")
    assert first["status"] == "REUSED"
    assert fake.prepares == 1
    left = coordinator.acquire_slot("left", overlay_tree_hash="tree", dirty_files=("a.py",))
    right = coordinator.acquire_slot("right", overlay_tree_hash="tree", dirty_files=("b.py",))
    assert left["slot"]["overlay_key"] != right["slot"]["overlay_key"]
    coordinator.record_candidate("left", "candidate-z", _changeset(), verified=False)
    coordinator.record_candidate("right", "candidate-a", _changeset(), verified=True)
    result = coordinator.promote_winner()
    assert result["status"] == "PROMOTED"
    assert result["winner"] == "candidate-a"
    assert result["losers_skipped"] == ["candidate-z"]
    assert fake.applies == [(True, "g1", "ctx1")]
    assert coordinator.status()["metrics"]["canonical_builds"] == 1


def test_stale_checkpoint_is_rejected_and_refresh_invalidates_candidates(tmp_path):
    fake = _FakeIntegration()
    coordinator = FastFanoutCoordinator(tmp_path, integration=fake)
    coordinator.prepare("change app")
    coordinator.acquire_slot("slot", overlay_tree_hash="tree")
    with pytest.raises(FastFanoutError, match="stale"):
        coordinator.checkpoint("slot", generation="old")
    coordinator.record_candidate("slot", "candidate", _changeset(), verified=True)
    refreshed = coordinator.invalidate(source_commit="new-head")
    assert refreshed["status"] == "INVALIDATED"
    assert coordinator.generation == "g2"
    assert coordinator.context_hash == "ctx2"
    assert coordinator.select_winner()["status"] == "BLOCKED"
    assert fake.refreshes == 1


def test_snapshot_restores_slots_and_candidates_without_prepare(tmp_path):
    fake = _FakeIntegration()
    coordinator = FastFanoutCoordinator(tmp_path, integration=fake)
    coordinator.prepare("change app")
    coordinator.acquire_slot("slot", overlay_tree_hash="tree")
    coordinator.record_candidate("slot", "candidate", _changeset(), verified=True)
    restored = FastFanoutCoordinator.from_snapshot(tmp_path, coordinator.snapshot(), integration=fake)
    assert restored.status()["active_slots"] == ["slot"]
    assert restored.select_winner()["winner"] == "candidate"
    assert fake.prepares == 1


def test_snapshot_rejects_unknown_winner(tmp_path):
    fake = _FakeIntegration()
    coordinator = FastFanoutCoordinator(tmp_path, integration=fake)
    coordinator.prepare("change app")
    snapshot = coordinator.snapshot()
    snapshot["winner"] = "missing"
    with pytest.raises(FastFanoutError, match="winner"):
        FastFanoutCoordinator.restore(tmp_path, snapshot, integration=fake)


def test_rollout_modes_and_disable_gate_promotion(tmp_path):
    fake = _FakeIntegration()
    coordinator = FastFanoutCoordinator(tmp_path, integration=fake)
    coordinator.prepare("change app")
    for mode in ("shadow", "canary", "integrated"):
        receipt = coordinator.transition_rollout(mode, reason="test")
        assert receipt["status"] == "ROLLOUT_UPDATED"
        assert receipt["rollout"]["mode"] == mode
    disabled = coordinator.transition_rollout("disable", reason="operator stop")
    assert disabled["rollout"]["fast_mode"] == "fallback"
    assert coordinator.snapshot()["rollout"]["mode"] == "disable"
    with pytest.raises(FastFanoutError, match="unsupported rollout"):
        coordinator.transition_rollout("unknown")
