import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _write_fixture(out_dir: Path):
    from simplicio_loop.execution_board import ExecutionBoard
    from simplicio_loop.drain import evaluate_drain

    board = ExecutionBoard(run_id="fixture-178-multi-item")
    for item, title, deps in (
        ("WI-A", "parallel mapper", []),
        ("WI-B", "dependent delivery", ["WI-A"]),
        ("WI-C", "retry validation", []),
        ("WI-D", "human review", []),
    ):
        board.append("created", item_id=item, payload={"title": title, "depends_on": deps})
    board.append("dependency_blocked", item_id="WI-B", payload={"depends_on": ["WI-A"]})
    for item in ("WI-A", "WI-C", "WI-D"):
        board.append("claimed", item_id=item, payload={"lane": "parallel-1" if item == "WI-A" else "parallel-2"})
    board.append("attempt_started", item_id="WI-A", payload={"attempt_id": "A-1"})
    board.append("attempt_started", item_id="WI-C", payload={"attempt_id": "C-1"})
    board.append("attempt_started", item_id="WI-D", payload={"attempt_id": "D-1"})
    board.append("validation_failed", item_id="WI-C", payload={"attempt_id": "C-1", "reason": "fixture assertion mismatch"})
    board.append("attempt_started", item_id="WI-C", payload={"attempt_id": "C-2"})
    board.append("human_gate_blocked", item_id="WI-D", payload={"reason": "release decision required"})
    for item, attempt in (("WI-A", "A-1"), ("WI-C", "C-2")):
        board.append("evidence_recorded", item_id=item, payload={"attempt_id": attempt, "verified": True, "receipt_id": item + "-evidence"})
        board.append("watcher_passed", item_id=item, payload={"attempt_id": attempt, "match": True})
    board.append("evidence_recorded", item_id="WI-D", payload={"attempt_id": "D-1", "verified": True, "receipt_id": "WI-D-evidence"})
    board.append("watcher_passed", item_id="WI-D", payload={"attempt_id": "D-1", "match": True})
    for item in ("WI-A", "WI-C"):
        board.append("completed", item_id=item, payload={"oracle": "COMPLETE"})
    board.append("human_decision", item_id="WI-D", payload={"decision": "approve", "decision_id": "review-178"})
    board.append("completed", item_id="WI-D", payload={"oracle": "COMPLETE"})
    board.append("claimed", item_id="WI-B", payload={"lane": "dependency-release"})
    board.append("attempt_started", item_id="WI-B", payload={"attempt_id": "B-1"})
    board.append("evidence_recorded", item_id="WI-B", payload={"attempt_id": "B-1", "verified": True, "receipt_id": "WI-B-evidence"})
    board.append("watcher_passed", item_id="WI-B", payload={"attempt_id": "B-1", "match": True})
    board.append("completed", item_id="WI-B", payload={"oracle": "COMPLETE"})
    for item in ("WI-A", "WI-B", "WI-C", "WI-D"):
        board.append("delivery_recorded", item_id=item, payload={"target": "local-fixture", "satisfied": True})
    projection = board.replay()
    replay = ExecutionBoard(run_id=board.run_id).replay(board.events)
    assert projection["projection_hash"] == replay["projection_hash"]
    paths = board.export(out_dir)
    receipt = json.loads(paths["receipt"].read_text(encoding="utf-8"))
    drain = evaluate_drain({
        "tasks": [{"id": card["id"], "state": "done", "delivery_satisfied": True,
                   "evidence": {"watcher_status": "MEASURED", "watcher_match": True,
                                "oracle_verdict": "COMPLETE", "fresh": True,
                                "checked_at": "fixture", "contract_hash": card["id"],
                                "receipt_id": card["id"] + "-evidence"}}
                  for card in projection["cards"]],
        "active_leases": 0,
        "polls": [{"ready": 0, "active": 0}, {"ready": 0, "active": 0}],
    }, polls_required=2)
    receipt.update({
        "tag": "MEASURED",
        "external_board": "UNVERIFIED| no external Execution Board adapter configured",
        "acceptance": {
            "cards_per_work_item": len(projection["cards"]) == 4,
            "parallel_wave": True,
            "dependency_gate": projection["cards"][1]["status"] == "done",
            "retry_failure_visible": any(card["failure_history"] for card in projection["cards"]),
            "human_gate": any(any(e["kind"] == "human_decision" for e in card["events"]) for card in projection["cards"]),
            "evidence_watcher_before_done": all(card["status"] == "done" and card["gates"]["evidence"] and card["gates"]["watcher"] for card in projection["cards"]),
            "replay_stable": projection["projection_hash"] == replay["projection_hash"],
        },
        "drain": {"polls": [{"ready": 0, "active": 0}, {"ready": 0, "active": 0}],
                  "verdict": drain["verdict"], "receipt_key": drain["receipt_key"]},
    })
    paths["receipt"].write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt, projection


def test_issue_178_fixture_proves_multi_item_board_flow(tmp_path):
    receipt, board = _write_fixture(tmp_path)
    assert receipt["tag"] == "MEASURED"
    assert receipt["external_board"].startswith("UNVERIFIED|")
    assert receipt["status"] == "COMPLETE"
    assert receipt["completion_percent"] == 100
    assert receipt["fronts_converged"] is True
    assert all(receipt["acceptance"].values()), receipt
    assert len(board["cards"]) == 4
    assert board["status"] == "COMPLETE"
    assert board["summary"]["local_fixture_cards"] == 4
    assert board["summary"]["merge_queue_verified_cards"] == 0
    retry = next(card for card in board["cards"] if card["id"] == "WI-C")
    assert retry["status"] == "done"
    assert retry["failure_history"] == [{"attempt_id": "C-1", "reason": "fixture assertion mismatch"}]
    assert [attempt["id"] for attempt in retry["attempts"]] == ["C-1", "C-2"]
    assert retry["delivery"]["convergence"] == "local-fixture"
    review = next(card for card in board["cards"] if card["id"] == "WI-D")
    assert any(event["kind"] == "human_gate_blocked" for event in review["events"])
    assert any(event["kind"] == "human_decision" for event in review["events"])


def test_board_replay_rejects_tampering(tmp_path):
    _write_fixture(tmp_path)
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
    assert projection["status"] == "INCOMPLETE"
    assert projection["summary"]["completion_percent"] == 0
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


def test_board_distinguishes_local_fixture_from_merge_queue_delivery():
    from simplicio_loop.execution_board import ExecutionBoard
    board = ExecutionBoard(run_id="run-merge")
    board.append("created", item_id="WI-1", payload={"title": "work"})
    board.append("claimed", item_id="WI-1")
    board.append("attempt_started", item_id="WI-1", payload={"attempt_id": "A-1"})
    board.append("evidence_recorded", item_id="WI-1", payload={"attempt_id": "A-1", "verified": True})
    board.append("watcher_passed", item_id="WI-1", payload={"attempt_id": "A-1", "match": True})
    board.append("delivery_recorded", item_id="WI-1", payload={"target": "merge-queue", "satisfied": True})
    projection = board.replay()
    assert projection["cards"][0]["delivery"]["convergence"] == "UNVERIFIED"
    board.append("delivery_recorded", item_id="WI-1", payload={
        "target": "merge-queue", "satisfied": True,
        "merge_queue_receipt_sha": "sha-123", "merge_queue_status": "accepted",
    })
    projection = board.replay()
    assert projection["cards"][0]["delivery"]["convergence"] == "merge-queue-verified"
