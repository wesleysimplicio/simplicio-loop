import json

import scripts.watcher_verify_drain as watcher


def test_drain_watcher_binds_stop_hook_challenge(tmp_path, monkeypatch):
    backlog = tmp_path / "backlog.jsonl"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    backlog.write_text(
        json.dumps({
            "kind": "master",
            "goal_fp": "goal-drain",
        })
        + "\n"
        + json.dumps({
            "kind": "item",
            "id": "wi1",
            "status": "blocked",
            "reason_code": "infra-dependent",
            "source_refs": ["github:owner/repo#1"],
            "run_dir": str(run_dir),
        })
        + "\n",
        encoding="utf-8",
    )
    watcher_state = tmp_path / "watcher_state.json"
    challenge = tmp_path / "watcher_challenge.json"
    monkeypatch.setattr(watcher, "BACKLOG", str(backlog))
    monkeypatch.setattr(watcher, "WATCHER_STATE", str(watcher_state))
    monkeypatch.setattr(watcher, "CHALLENGE", str(challenge))
    monkeypatch.setattr(watcher, "_open_issues", lambda repo: ([1], None))

    assert watcher.cmd_verify("owner/repo") == 0

    state = json.loads(watcher_state.read_text(encoding="utf-8"))
    current_challenge = json.loads(challenge.read_text(encoding="utf-8"))
    assert state["match"] is True
    assert state["status"] == "MEASURED"
    assert state["challenge"] == current_challenge["challenge"]
    assert state["goal_fp"] == current_challenge["goal_fp"] == "goal-drain"
