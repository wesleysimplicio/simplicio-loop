import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.distributed_183_e2e import run  # noqa: E402


def test_issue_183_ac7_local_fixture_proves_isolated_merge_queue_flow(tmp_path):
    result = run(tmp_path)
    receipt = result["receipt"]
    projection = result["projection"]
    queue_state = result["queue_state"]

    assert receipt["tag"] == "MEASURED"
    assert receipt["issue"] == 183
    assert receipt["slice"] == "AC7"
    assert receipt["epic_closure_ready"] is False
    assert all(receipt["acceptance"].values()), receipt["acceptance"]

    assert projection["status"] == "COMPLETE"
    assert projection["summary"]["completion_percent"] == 100
    assert projection["summary"]["merge_queue_verified_cards"] == 2
    assert all(card["delivery"]["convergence"] == "merge-queue-verified" for card in projection["cards"])

    worktree_receipts = receipt["local_measured"]["worktree_receipts"]
    assert len(worktree_receipts) == 2
    assert len({row["path"] for row in worktree_receipts}) == 2
    assert len({row["branch"] for row in worktree_receipts}) == 2

    merge_receipts = receipt["local_measured"]["merge_queue_receipts"]
    assert len(merge_receipts) == 2
    assert all(row["merge_queue_receipt_sha"] for row in merge_receipts)
    assert all(Path(row["merge_queue_receipt_path"]).exists() for row in merge_receipts)
    assert all(row["merge_queue_status"] == "accepted" for row in merge_receipts)

    assert len(queue_state["claims"]) == 2
    assert all(task["status"] == "done" for task in queue_state["tasks"].values())
    assert all(task["context_pack"]["issue_ref"] == "wesleysimplicio/simplicio-loop#183"
               for task in queue_state["tasks"].values())
    assert receipt["external_unverified"]["physical_multi_machine"].startswith("UNVERIFIED|")
    assert Path(result["artifact_paths"]["receipt"]).exists()
