import json

import scripts.watcher_verify as watcher


def test_watcher_rejects_run_from_different_commit(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    loop = repo / ".orchestrator" / "loop"
    run = repo / ".orchestrator" / "runs" / "r1"
    loop.mkdir(parents=True)
    run.mkdir(parents=True)
    watcher._set_repo(str(repo))
    monkeypatch.setenv("SIMPLICIO_RUN_DIR", str(run))
    monkeypatch.setattr(watcher, "_git_meta", lambda: {"commit_sha": "actual", "diff_hash": "same", "diff_present": False})
    (loop / "watcher_challenge.json").write_text(json.dumps({"challenge": "c1", "written_at": "2026-07-10T00:00:00Z"}), encoding="utf-8")
    (loop / "anchor.json").write_text(json.dumps({"criteria": [{"id": "AC1", "status": "done"}]}), encoding="utf-8")
    (run / "evidence-receipt.json").write_text(json.dumps({"schema": "simplicio.evidence-receipt/v1", "run_id": "r1", "status": "VERIFIED", "run": {"commit_sha": "deadbeef", "diff_hash": "same"}, "criteria": [{"id": "AC1", "verification_state": "verified"}], "summary": {"criteria_total": 1, "criteria_verified": 1, "scenario_total": 1, "scenario_verified": 1, "rule_total": 0, "rule_verified": 0}, "checks": []}), encoding="utf-8")
    assert watcher.cmd_verify() == 0
    state = json.loads((loop / "watcher_state.json").read_text(encoding="utf-8"))
    assert state["match"] is False
    assert "run commit differs" in state["reported"]
    assert state["match"] is False
    assert "run commit differs" in state["reported"]
