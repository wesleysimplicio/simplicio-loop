import json

from simplicio_loop.runner import _emit_event, _record_event, _transition
from simplicio_loop.progress import build_progress


def _state():
    return {
        "schema": "simplicio.run-state/v1",
        "run_id": "run-events",
        "phase": "executing",
        "task_ids": ["T1"],
        "ac_ids": ["AC-1"],
        "events": [],
        "history": [],
        "blockers": [],
    }


def test_runner_events_are_persisted_with_provenance_and_progress_can_render_them(tmp_path):
    run_dir = tmp_path / "run-events"
    run_dir.mkdir()
    state = _state()
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    _emit_event(run_dir, state, "contract_frozen", receipt="task-contract.json", message="frozen")
    _emit_event(run_dir, state, "mapper_fresh", receipt="mapper-context.json", message="fresh")
    _emit_event(run_dir, state, "plan_ready", receipt="plan.json", message="ready")
    _emit_event(run_dir, state, "worker_claimed", receipt="task-contract.json", message="claimed")
    _emit_event(run_dir, state, "test_gate", blocker="evidence_unverified", message="gate")

    payload = build_progress(state)
    assert [event["kind"] for event in payload["events"]] == [
        "contract_frozen", "mapper_fresh", "plan_ready", "worker_claimed", "test_gate"
    ]
    assert all(event["run_id"] == "run-events" for event in payload["events"])
    assert all(event["task_id"] == "T1" for event in payload["events"])
    assert all(event["ac_ids"] == ["AC-1"] for event in payload["events"])
    assert payload["events"][-1]["metadata_status"] == "MEASURED"
    assert "evidence_unverified" in payload["blockers"]
    persisted = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert len(persisted) == 5


def test_phase_transition_is_visible_without_claiming_completion(tmp_path):
    run_dir = tmp_path / "run-events"
    run_dir.mkdir()
    state = _state()
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    _transition(run_dir, state, "validating", "operator receipt persisted", receipt="operator.json")
    payload = build_progress(state)
    assert payload["phase"] == "validating"
    assert payload["events"][-1]["kind"] == "phase_transition"
    assert payload["events"][-1]["receipt"] == "operator.json"
    assert payload["status"] == "RUNNING"
    assert payload["percent"] < 100
