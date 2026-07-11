import io
import json

from simplicio_loop.progress import build_progress, render_markdown, render_text, stream


def _state(**overrides):
    state = {
        "schema": "simplicio.run-state/v1",
        "run_id": "run-demo",
        "phase": "executing",
        "task_count": 3,
        "coverage": {"scenarios": {"verified": 1, "total": 3}},
        "current_action": "worker_1",
        "next_action": "validate",
        "evidence": {"ready": False, "status": "UNVERIFIED"},
        "completion": {"ready": False, "verdict": "DELIVERY_PENDING"},
    }
    state.update(overrides)
    return state


def test_progress_is_visual_and_honest_before_receipt():
    event = build_progress(_state(phase="done"))
    assert event["percent"] == 99
    assert event["status"] == "RUNNING"
    assert event["gates"]["oracle"] is False
    text = render_text(event)
    assert "99%" in text and "░" in text


def test_progress_reaches_100_only_for_complete_oracle():
    event = build_progress(_state(phase="done", completion={"ready": True, "verdict": "COMPLETE"},
                                  evidence={"ready": True, "status": "VERIFIED"}))
    assert event["percent"] == 100
    assert event["status"] == "COMPLETE"
    assert event["gates"]["oracle"] is True
    assert "100%" in render_markdown(event)


def test_json_stream_is_machine_consumable(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "state.json").write_text(json.dumps(_state()), encoding="utf-8")
    out = io.StringIO()
    event = stream(run, fmt="json", once=True, out=out)
    assert event["schema"] == "simplicio.progress/v1"
    assert json.loads(out.getvalue())["run_id"] == "run-demo"


def test_completion_receipt_can_promote_state_to_100(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "state.json").write_text(json.dumps(_state(phase="delivering")), encoding="utf-8")
    (run / "completion-receipt.json").write_text(json.dumps({"ready": True, "verdict": "DRAINED"}), encoding="utf-8")
    out = io.StringIO()
    event = stream(run, fmt="json", once=True, out=out)
    assert event["percent"] == 100
    assert json.loads(out.getvalue())["status"] == "COMPLETE"
