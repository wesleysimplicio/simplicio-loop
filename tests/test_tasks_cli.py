from __future__ import annotations

import json

from simplicio_loop import cli

def test_tasks_run_dry_run_deduplicates_scope(tmp_path, capsys):
    scope = tmp_path / "items.json"
    scope.write_text(json.dumps(["issue-2", "issue-1", "issue-2"]), encoding="utf-8")

    assert cli.main(["tasks", "run", str(scope), "--dry-run"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "simplicio.tasks-run-plan/v1"
    assert payload["state"] == "partial"
    assert payload["deduplicated_count"] == 2
    assert [row["item"] for row in payload["items"]] == ["issue-1", "issue-2"]
    assert payload["items"][0]["pipeline"] == ["implement:coding-loop", "review:adversarial-review", "pr"]
    assert payload["items"][0]["worktree_isolation"] is True
    assert payload["items"][0]["evidence"] == {"pr": None, "verification": None}

def test_tasks_run_requires_action_gate(capsys):
    assert cli.main(["tasks", "run"]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "blocked"
    assert payload["reason"] == "action_gate_required"

def test_tasks_run_rejects_non_list_scope(tmp_path, capsys):
    scope = tmp_path / "items.json"
    scope.write_text(json.dumps({"item": "issue-1"}), encoding="utf-8")

    assert cli.main(["tasks", "run", str(scope), "--dry-run"]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "blocked"
    assert "JSON array" in payload["reason"]
