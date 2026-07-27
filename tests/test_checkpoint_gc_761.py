import json

from simplicio_loop.checkpoints import build_checkpoint, write_checkpoint
from simplicio_loop.cli import main


def _write_checkpoint(root, state, shard_id):
    value = build_checkpoint(
        task_id="task-761", attempt_id="attempt-1", candidate_id="candidate-1",
        shard_id=shard_id, state=state, repo=str(root), source_commit="commit-1",
        fast_generation="SFAST001:generation", snapshot_sha256="snapshot-1",
        handles=["handle-1"], receipts=["receipt-1"],
    )
    path = root / f"{shard_id}.json"
    write_checkpoint(path, value)
    return path


def test_gc_requires_active_lease_and_applies_cancelled_only(tmp_path, capsys):
    directory = tmp_path / ".simplicio" / "loop-runs"
    directory.mkdir(parents=True)
    cancelled = _write_checkpoint(directory, "CANCELLED", "cancelled")
    ready = _write_checkpoint(directory, "READY_TO_PROMOTE", "ready")
    lease = directory / "gc-lease.json"
    lease.write_text(json.dumps({"lease_id": "lease-1", "status": "ACTIVE", "scope": str(directory.resolve())}), encoding="utf-8")

    assert main(["checkpoint", "gc", "--directory", str(directory), "--lease-file", str(lease), "--retention-seconds", "0"]) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["status"] == "DRY_RUN"
    assert str(cancelled) in [row["path"] for row in dry_run["eligible"]]
    assert cancelled.exists() and ready.exists()

    assert main(["checkpoint", "gc", "--directory", str(directory), "--lease-file", str(lease), "--retention-seconds", "0", "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "GC_APPLIED"
    assert str(cancelled) in applied["removed"]
    assert not cancelled.exists()
    assert ready.exists()


def test_gc_without_lease_is_held(tmp_path, capsys):
    directory = tmp_path / ".simplicio" / "loop-runs"
    directory.mkdir(parents=True)
    assert main(["checkpoint", "gc", "--directory", str(directory)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "HELD"
    assert payload["reason_code"] == "gc_requires_active_lease"
