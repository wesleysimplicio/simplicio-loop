from __future__ import annotations

import json

from simplicio_loop import cli

def test_tasks_run_dry_run_executes_real_discovery(monkeypatch, tmp_path, capsys):
    from simplicio_loop import tasks_live
    captured = {}
    def fake(request, **kwargs):
        captured["request"] = request
        captured.update(kwargs)
        return {"schema": "simplicio.tasks-orchestrator/v1", "state": "partial", "plan": {"items": {"7": {"state": "planned"}}}}
    monkeypatch.setattr(tasks_live, "run_live", fake)
    request = "finish all issues in acme/widgets"
    assert cli.main(["tasks", "run", request, "--workspace", str(tmp_path), "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["plan"]["items"]["7"]["state"] == "planned"
    assert captured["request"] == request
    assert captured["action_gate"] is False
    assert captured["agent_command"] == ()

def test_tasks_run_requires_action_gate(capsys):
    assert cli.main(["tasks", "run"]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "blocked"
    assert payload["reason"] == "action_gate_required"

def test_tasks_run_dry_run_reports_discovery_failure(monkeypatch, capsys):
    from simplicio_loop import tasks_live
    monkeypatch.setattr(tasks_live, "run_live", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("discovery failed")))
    assert cli.main(["tasks", "run", "all issues", "--dry-run"]) == 2
    assert "discovery failed" in json.loads(capsys.readouterr().out)["reason"]


def test_tasks_cancel_forwards_without_action_gate_or_agent_command(monkeypatch, tmp_path, capsys):
    from simplicio_loop import tasks_live
    captured = {}
    def fake(request, **kwargs):
        captured.update(kwargs)
        return {"state": "cancelled"}
    monkeypatch.setattr(tasks_live, "run_live", fake)
    assert cli.main(["tasks", "run", "finish all issues in acme/widgets", "--workspace", str(tmp_path), "--cancel"]) == 3
    assert captured["cancel"] is True
    assert json.loads(capsys.readouterr().out)["state"] == "cancelled"
