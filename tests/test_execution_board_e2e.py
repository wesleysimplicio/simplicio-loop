import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "execution_board_e2e.py"


def test_issue_178_fixture_proves_multi_item_board_flow(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(tmp_path)],
        cwd=str(REPO), capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads((tmp_path / "execution-board-receipt.json").read_text(encoding="utf-8"))
    assert receipt["tag"] == "MEASURED"
    assert receipt["external_board"].startswith("UNVERIFIED|")
    assert all(receipt["acceptance"].values()), receipt
    board = json.loads((tmp_path / "execution-board.json").read_text(encoding="utf-8"))
    assert len(board["cards"]) == 4
    retry = next(card for card in board["cards"] if card["id"] == "WI-C")
    assert retry["status"] == "done"
    assert retry["failure_history"] == [{"attempt_id": "C-1", "reason": "fixture assertion mismatch"}]
    assert [attempt["id"] for attempt in retry["attempts"]] == ["C-1", "C-2"]
    review = next(card for card in board["cards"] if card["id"] == "WI-D")
    assert any(event["kind"] == "human_gate_blocked" for event in review["events"])
    assert any(event["kind"] == "human_decision" for event in review["events"])


def test_board_replay_rejects_tampering(tmp_path):
    subprocess.run([sys.executable, str(SCRIPT), "--out", str(tmp_path)], cwd=str(REPO), check=True)
    lines = (tmp_path / "execution-board-events.jsonl").read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[0])
    event["payload"]["title"] = "tampered"
    lines[0] = json.dumps(event)
    (tmp_path / "execution-board-events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    # The exported board remains the immutable receipt; a consumer replaying the altered stream
    # through ExecutionBoard must reject it (covered directly to keep the failure mode explicit).
    from simplicio_loop.execution_board import BoardError, ExecutionBoard
    with __import__("pytest").raises(BoardError):
        ExecutionBoard(run_id="fixture-178-multi-item").replay([json.loads(line) for line in lines])


def test_board_imports_frozen_workitems_losslessly_and_deterministically(tmp_path):
    items = [
        {"kind": "item", "id": "WI-B", "goal": "Ship B", "goal_fp": "fp-b",
         "acs": ["B is verified"], "status": "ready", "depends_on": ["WI-A"],
         "source_refs": [{"path": "README.md", "sha1": "abc"}],
         "required_evidence": ["watcher"], "risks": ["remote"], "estimate": 3,
         "scheduling_hints": {"lane": "parallel"}, "extra": {"owner": "runtime"}},
        {"kind": "item", "id": "WI-A", "goal": "Ship A", "goal_fp": "fp-a",
         "acs": ["A is verified"], "status": "done", "depends_on": [],
         "source_refs": [], "required_evidence": ["receipt"], "risks": [],
         "estimate": 1, "scheduling_hints": {}},
    ]
    master = {"kind": "master", "schema": "simplicio.backlog/v2", "goal": "release",
              "goal_fp": "goal-fp", "contract": {"name": "simplicio.work-items/v1"}}
    path = tmp_path / "backlog.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in [master] + items) + "\n", encoding="utf-8")
    from simplicio_loop.execution_board import BoardError, ExecutionBoard
    first = ExecutionBoard.from_backlog(path, run_id="run-import")
    second = ExecutionBoard.from_backlog(path, run_id="run-import")
    projection = first.replay()
    assert projection["external_status"] == "UNVERIFIED"
    assert [card["id"] for card in projection["cards"]] == ["WI-A", "WI-B"]
    imported = {card["id"]: card["work_item"] for card in projection["cards"]}
    assert imported["WI-B"] == items[0]
    assert projection["cards"][1]["required_evidence"] == ["watcher"]
    assert projection["cards"][1]["depends_on"] == ["WI-A"]
    assert projection["projection_hash"] == second.replay()["projection_hash"]
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"kind": "master", "schema": "unknown"}) + "\n", encoding="utf-8")
    with __import__("pytest").raises(BoardError):
        ExecutionBoard.from_backlog(bad)
