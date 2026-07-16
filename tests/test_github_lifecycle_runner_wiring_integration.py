"""Tests for the #285 GitHub lifecycle sync wired into the runner's event stream
(`simplicio_loop/runner.py::_record_event` -> `_sync_github_lifecycle`).

Enabled by default for runs carrying a `source_issue` and fail-open: an explicit
falsy `SIMPLICIO_LOOP_GITHUB_LIFECYCLE_SYNC=0` is the temporary opt-out. No
`source_issue` on the run state, or any transport error must never break
`_record_event`/`_emit_event`, which the whole loop depends on for every phase.
"""
import json

from simplicio_loop import github_lifecycle
from simplicio_loop.runner import _emit_event


def _state(with_source_issue=False):
    state = {
        "schema": "simplicio.run-state/v1",
        "run_id": "run-wiring",
        "phase": "executing",
        "task_ids": ["T1"],
        "ac_ids": ["AC-1"],
        "events": [],
        "history": [],
        "blockers": [],
    }
    if with_source_issue:
        state["source_issue"] = {"owner": "acme", "repo": "widgets", "issue": "12"}
    return state


def test_sync_is_a_noop_with_explicit_legacy_opt_out(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPLICIO_LOOP_GITHUB_LIFECYCLE_SYNC", "0")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    state = _state(with_source_issue=True)
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    calls = []
    monkeypatch.setattr(github_lifecycle, "publish_lifecycle_state",
                        lambda **kw: calls.append(kw) or {"verified": True})
    _emit_event(run_dir, state, "worker_claimed", message="claimed")
    assert calls == []
    assert not (run_dir / "lifecycle-sync-errors.jsonl").exists()


def test_sync_is_a_noop_without_a_source_issue(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPLICIO_LOOP_GITHUB_LIFECYCLE_SYNC", "1")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    state = _state(with_source_issue=False)
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    calls = []
    monkeypatch.setattr(github_lifecycle, "publish_lifecycle_state",
                        lambda **kw: calls.append(kw) or {"verified": True})
    _emit_event(run_dir, state, "worker_claimed", message="claimed")
    assert calls == []


def test_sync_projects_a_mapped_event_onto_the_lifecycle_state(tmp_path, monkeypatch):
    monkeypatch.delenv("SIMPLICIO_LOOP_GITHUB_LIFECYCLE_SYNC", raising=False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    state = _state(with_source_issue=True)
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    calls = []
    monkeypatch.setattr(github_lifecycle, "publish_lifecycle_state",
                        lambda **kw: calls.append(kw) or {"verified": True})
    _emit_event(run_dir, state, "worker_claimed", message="claimed")

    assert len(calls) == 1
    assert calls[0]["owner"] == "acme"
    assert calls[0]["repo"] == "widgets"
    assert calls[0]["issue"] == "12"
    assert calls[0]["state"] == "CLAIMED"
    assert calls[0]["run_id"] == "run-wiring"


def test_sync_is_a_noop_for_an_unmapped_event_kind(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPLICIO_LOOP_GITHUB_LIFECYCLE_SYNC", "1")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    state = _state(with_source_issue=True)
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    calls = []
    monkeypatch.setattr(github_lifecycle, "publish_lifecycle_state",
                        lambda **kw: calls.append(kw) or {"verified": True})
    _emit_event(run_dir, state, "done", message="finished")  # no lifecycle mapping -> no auto-close
    assert calls == []


def test_sync_failure_is_logged_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPLICIO_LOOP_GITHUB_LIFECYCLE_SYNC", "1")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    state = _state(with_source_issue=True)
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def _boom(**kw):
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(github_lifecycle, "publish_lifecycle_state", _boom)
    # Must not raise -- fail-open, exactly like pr_evidence.py's progress-comment.
    _emit_event(run_dir, state, "worker_claimed", message="claimed")

    log_path = run_dir / "lifecycle-sync-errors.jsonl"
    assert log_path.exists()
    logged = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert "network unavailable" in logged["error"]
