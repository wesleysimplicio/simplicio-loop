import json
from pathlib import Path

from simplicio_loop.progress import build_progress, render_markdown, render_text


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "contracts" / "progress" / "v1" / "snapshots.json"


def test_progress_snapshot_contract_covers_milestones_and_fanout():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert payload["schema"] == "simplicio.progress-snapshots/v1"
    assert {case["id"] for case in payload["cases"]} >= {
        "single-0", "single-25", "single-50", "single-75", "single-100", "fanout-blocked"
    }
    for case in payload["cases"]:
        event = build_progress(case["state"])
        expected = case["expected"]
        assert {key: event[key] for key in ("percent", "status")} == {
            key: expected[key] for key in ("percent", "status")
        }, case["id"]
        assert event["gates"]["oracle"] is expected["oracle"], case["id"]
        if "lanes" in expected:
            assert len(event["lanes"]) == expected["lanes"]
        if "events" in expected:
            assert len(event["events"]) == expected["events"]


def test_progress_renderers_preserve_gates_actions_and_no_control_codes():
    state = next(case["state"] for case in json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"] if case["id"] == "fanout-blocked")
    event = build_progress(state)
    markdown = render_markdown(event, ascii_only=True)
    plain = render_text(event, ascii_only=True)
    for output in (markdown, plain):
        assert "watcher_challenge" in output
        assert "reconcile" in output
        assert "\x1b" not in output
    assert "Lanes:" in markdown


def test_unverified_percent_100_is_clamped_until_oracle_receipt():
    state = {"run_id": "stale", "phase": "executing", "progress_percent": 100,
             "task_count": 1, "completion": {"ready": False, "verdict": "DELIVERY_PENDING"}}
    event = build_progress(state)
    assert event["percent"] == 99
    assert event["status"] == "RUNNING"
    assert event["gates"]["oracle"] is False
