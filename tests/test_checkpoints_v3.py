from __future__ import annotations

import pytest

from simplicio_loop.checkpoints import (
    CHECKPOINT_SCHEMA,
    FANIN_SCHEMA,
    PROMOTION_FENCE_SCHEMA,
    CheckpointError,
    build_checkpoint,
    fanin_checkpoints,
    promote_winner,
    read_checkpoint,
    verify_checkpoint,
    write_checkpoint,
)


def _checkpoint(shard_id: str, *, state: str = "READY_TO_PROMOTE", previous_digest: str | None = None):
    return build_checkpoint(
        task_id="task-761",
        attempt_id="attempt-1",
        candidate_id="candidate-a",
        shard_id=shard_id,
        state=state,
        repo="repo",
        source_commit="commit-a",
        fast_generation="SFAST001:generation-a",
        snapshot_sha256="snapshot-a",
        capabilities={"fast_engine": "rust"},
        handles=["handle-b", "handle-a"],
        receipts=["receipt-b", "receipt-a"],
        previous_digest=previous_digest,
        created_at="2026-07-27T00:00:00Z",
    )


def test_checkpoint_is_bounded_and_hash_verified():
    value = _checkpoint("shard-1")
    assert value["schema"] == CHECKPOINT_SCHEMA
    assert value["handles"] == ["handle-a", "handle-b"]
    assert value["receipts"] == ["receipt-a", "receipt-b"]
    assert verify_checkpoint(value)["checkpoint_digest"] == value["checkpoint_digest"]


def test_atomic_checkpoint_round_trip_and_identity_revalidation(tmp_path):
    path = tmp_path / "checkpoint.json"
    value = write_checkpoint(path, _checkpoint("shard-1"))
    assert read_checkpoint(path, expected_identity={"fast_generation": "SFAST001:generation-a"}) == value
    with pytest.raises(CheckpointError, match="stale checkpoint identity"):
        read_checkpoint(path, expected_identity={"fast_generation": "SFAST001:stale"})


def test_corrupt_checkpoint_and_effect_without_receipt_fail_closed():
    value = _checkpoint("shard-1")
    value["checkpoint_digest"] = "0" * 64
    with pytest.raises(CheckpointError, match="checkpoint_digest mismatch"):
        verify_checkpoint(value)
    with pytest.raises(CheckpointError, match="effect_receipt_digest"):
        _checkpoint("shard-1", state="APPLIED")


def test_fanin_requires_all_terminal_unique_shards_and_is_deterministic():
    first = _checkpoint("shard-1")
    second = _checkpoint("shard-2", previous_digest=first["checkpoint_digest"])
    result = fanin_checkpoints([second, first], expected_shard_ids=["shard-2", "shard-1"])
    assert result["schema"] == FANIN_SCHEMA
    assert result["shard_ids"] == ["shard-1", "shard-2"]
    assert result["status"] == "READY"
    with pytest.raises(CheckpointError, match="duplicate shard"):
        fanin_checkpoints([first, first], expected_shard_ids=["shard-1", "shard-2"])
    with pytest.raises(CheckpointError, match="shard mismatch"):
        fanin_checkpoints([first], expected_shard_ids=["shard-1", "shard-2"])


def test_fanin_rejects_non_terminal_shard():
    with pytest.raises(CheckpointError, match="not terminal"):
        fanin_checkpoints([_checkpoint("shard-1", state="PLANNED")], expected_shard_ids=["shard-1"])


def test_winner_fence_is_single_writer_and_idempotent(tmp_path):
    candidates = [
        {"candidate_id": "candidate-a", "status": "verified", "candidate_digest": "digest-a"},
        {"candidate_id": "candidate-b", "status": "verified", "candidate_digest": "digest-b"},
    ]
    path = tmp_path / "promotion.json"
    value = promote_winner(candidates, winner_id="candidate-a", fence_path=path)
    assert value["schema"] == PROMOTION_FENCE_SCHEMA
    assert promote_winner(candidates, winner_id="candidate-a", fence_path=path) == value
    with pytest.raises(CheckpointError, match="another winner"):
        promote_winner(candidates, winner_id="candidate-b", fence_path=path)


def test_winner_must_be_verified(tmp_path):
    with pytest.raises(CheckpointError, match="not verified"):
        promote_winner([{"candidate_id": "candidate-a", "status": "proposed", "candidate_digest": "digest-a"}], winner_id="candidate-a", fence_path=tmp_path / "promotion.json")
