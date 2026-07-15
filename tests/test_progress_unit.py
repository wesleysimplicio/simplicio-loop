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


def test_receipts_refresh_stale_state_gate_indicators(tmp_path):
    run = tmp_path / "run"
    (run / "loop").mkdir(parents=True)
    (run / "state.json").write_text(json.dumps(_state(phase="watching")), encoding="utf-8")
    (run / "evidence-receipt.json").write_text(json.dumps({"status": "VERIFIED"}), encoding="utf-8")
    (run / "loop" / "watcher_state.json").write_text(json.dumps({"status": "MEASURED", "match": True}), encoding="utf-8")
    event = build_progress(_state(phase="watching"), run_dir=run)
    assert event["gates"]["evidence"] is True
    assert event["gates"]["watcher"] is True


def test_fanout_lanes_and_phase_events_are_portable():
    event = build_progress(_state(
        lanes=[{"id": "worker-a", "status": "running", "percent": 50, "worktree": "wt/a"},
                {"id": "worker-b", "status": "blocked", "percent": 25}],
        events=[{"phase": "worker_claimed", "task_id": "T1", "status": "ok"}],
    ))
    assert event["lanes"][0]["id"] == "worker-a"
    assert event["events"][0]["task_id"] == "T1"
    assert "worker-a" in render_text(event)
    assert "Lanes:" in render_markdown(event)


def test_ascii_static_mode_has_no_control_codes_or_unicode():
    event = build_progress(_state())
    rendered = render_text(event, ascii_only=True)
    assert "\x1b" not in rendered
    assert "█" not in rendered and "▫️" not in rendered
    assert "[run]" in rendered


def test_no_animation_emits_one_plain_snapshot(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "state.json").write_text(json.dumps(_state()), encoding="utf-8")
    out = io.StringIO()
    event = stream(run, fmt="ansi", no_animation=True, ascii_only=True, out=out)
    assert event["status"] == "RUNNING"
    assert "\x1b" not in out.getvalue()
    assert out.getvalue().count("ação:") == 1


def test_non_tty_ansi_format_degrades_to_plain_text(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "state.json").write_text(json.dumps(_state()), encoding="utf-8")
    out = io.StringIO()
    stream(run, fmt="ansi", once=True, out=out)
    assert "\x1b" not in out.getvalue()


def test_event_provenance_is_preserved_and_missing_evidence_is_explicit():
    event = build_progress(_state(events=[{
        "event_id": "evt-1", "kind": "test_gate", "task_id": "T1", "ac_ids": ["AC-1"],
        "receipt_ref": "test-receipt.json", "status": "ok",
    }, {"kind": "watcher_challenge", "task_id": "T1"}]))
    measured, unverified = event["events"]
    assert measured["run_id"] == "run-demo"
    assert measured["ac_ids"] == ["AC-1"]
    assert measured["receipt"] == "test-receipt.json"
    assert measured["metadata_status"] == "MEASURED"
    assert unverified["metadata_status"] == "UNVERIFIED"
    assert "missing_event_metadata" in unverified["blocker"]
    assert unverified["blocker"] in event["blockers"]


def test_cancelled_run_is_terminal_and_honest():
    event = build_progress(_state(phase="cancelled", progress_percent=100))
    assert event["status"] == "CANCELLED"
    assert event["percent"] == 0
