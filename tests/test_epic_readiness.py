from simplicio_loop.epic_readiness import evaluate_epic_readiness


def test_epic_is_blocked_by_open_or_unevidenced_children():
    report = evaluate_epic_readiness([
        {"number": 675, "state": "CLOSED", "merged_pr": 699, "verification": "tests"},
        {"number": 676, "state": "OPEN", "merged_pr": 706, "verification": "tests"},
    ], required=(675, 676, 677))
    assert report["status"] == "BLOCKED"
    assert "open:676" in report["reasons"]
    assert "missing:677" in report["reasons"]


def test_epic_ready_requires_all_children_and_receipts():
    children = [{"number": number, "state": "CLOSED", "merged_pr": number + 1, "verification": "local"} for number in (1, 2)]
    report = evaluate_epic_readiness(children, required=(1, 2))
    assert report["status"] == "READY"
    assert len(report["audit_hash"]) == 64
