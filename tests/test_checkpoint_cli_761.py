import json

from simplicio_loop.checkpoints import build_checkpoint, write_checkpoint
from simplicio_loop.cli import main


def _checkpoint(tmp_path, shard_id="shard-1"):
    value = build_checkpoint(
        task_id="task-1",
        attempt_id="attempt-1",
        candidate_id="candidate-1",
        shard_id=shard_id,
        state="READY_TO_PROMOTE",
        repo=str(tmp_path),
        source_commit="commit-1",
        fast_generation="SFAST001:generation",
        snapshot_sha256="snapshot-1",
        handles=["handle-1"],
        receipts=["receipt-1"],
    )
    path = tmp_path / f"{shard_id}.json"
    write_checkpoint(path, value)
    return path, value


def test_checkpoint_verify_and_resume_are_read_only(tmp_path, capsys):
    path, value = _checkpoint(tmp_path)
    assert main(["checkpoint", "verify", "--path", str(path)]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["status"] == "VALID"
    assert verified["checkpoint"]["checkpoint_digest"] == value["checkpoint_digest"]

    assert main(["checkpoint", "resume", "--path", str(path)]) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["status"] == "RESUME_READY"
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "READY_TO_PROMOTE"


def test_checkpoint_cancel_chains_digest_without_mutating_source(tmp_path, capsys):
    path, value = _checkpoint(tmp_path)
    out = tmp_path / "cancelled.json"
    assert main(
        ["checkpoint", "cancel", "--path", str(path), "--out", str(out)]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    cancelled = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == "CANCELLED"
    assert cancelled["state"] == "CANCELLED"
    assert cancelled["previous_digest"] == value["checkpoint_digest"]
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "READY_TO_PROMOTE"


def test_checkpoint_fanin_and_seal_are_deterministic(tmp_path, capsys):
    first_path, first = _checkpoint(tmp_path, "shard-1")
    second = dict(first)
    second["shard_id"] = "shard-2"
    second["checkpoint_id"] = ""
    second["checkpoint_digest"] = ""
    second = build_checkpoint(
        task_id=first["task_id"],
        attempt_id=first["attempt_id"],
        candidate_id=first["candidate_id"],
        shard_id="shard-2",
        state="READY_TO_PROMOTE",
        repo=first["repo"],
        source_commit=first["source_commit"],
        fast_generation=first["fast_generation"],
        snapshot_sha256=first["snapshot_sha256"],
        handles=first["handles"],
        receipts=first["receipts"],
    )
    second_path = tmp_path / "shard-2.json"
    write_checkpoint(second_path, second)
    fanin_out = tmp_path / "fanin.json"
    assert main(
        [
            "checkpoint",
            "fanin",
            "--path",
            str(first_path),
            "--path",
            str(second_path),
            "--expected-shard-id",
            "shard-1",
            "--expected-shard-id",
            "shard-2",
            "--out",
            str(fanin_out),
        ]
    ) == 0
    fanin = json.loads(capsys.readouterr().out)
    assert fanin["status"] == "READY"
    assert json.loads(fanin_out.read_text(encoding="utf-8"))["status"] == "READY"

    candidates = tmp_path / "candidates.json"
    candidates.write_text(
        json.dumps(
            [
                {
                    "candidate_id": "candidate-1",
                    "candidate_digest": "digest-1",
                    "status": "verified",
                }
            ]
        ),
        encoding="utf-8",
    )
    fence = tmp_path / "promotion-fence.json"
    assert main(
        [
            "checkpoint",
            "seal",
            "--candidate-file",
            str(candidates),
            "--winner-id",
            "candidate-1",
            "--fence-path",
            str(fence),
        ]
    ) == 0
    sealed = json.loads(capsys.readouterr().out)
    assert sealed["status"] == "SEALED"
    assert fence.exists()


def test_checkpoint_list_and_gc_are_explicitly_bounded(tmp_path, capsys):
    path, _ = _checkpoint(tmp_path)
    assert main(
        ["checkpoint", "list", "--directory", str(tmp_path)]
    ) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["entries"][0]["status"] == "VALID"

    assert main(
        ["checkpoint", "gc", "--directory", str(tmp_path)]
    ) == 0
    gc = json.loads(capsys.readouterr().out)
    assert gc["status"] == "HELD"
    assert gc["removed"] == []
    assert str(path) in gc["candidates"]
