from __future__ import annotations

import json

from simplicio_loop import cli, github_drain_intake_cli


def test_top_level_routes_pt_br_and_legacy_drain_english_collision(monkeypatch):
    seen = []

    def fake_main(argv):
        seen.append(list(argv))
        return 17

    monkeypatch.setattr(github_drain_intake_cli, "main", fake_main)
    assert cli.main(["termine", "todas", "as", "issues", "de", "acme/widgets"]) == 17
    assert cli.main(["drain", "all", "issues", "in", "acme/widgets"]) == 17
    assert seen[0][0] == "termine"
    assert seen[1][0] == "drain"


def test_explicit_plan_subcommand_forwards_without_claiming_execution(monkeypatch):
    seen = {}

    def fake_main(argv):
        seen["argv"] = list(argv)
        return 18

    monkeypatch.setattr(github_drain_intake_cli, "main", fake_main)
    assert cli.main(["hub-drain-plan", "finish all issues in acme/widgets"]) == 18
    assert seen["argv"] == ["finish all issues in acme/widgets"]


def test_invalid_natural_request_is_structured_and_nonzero(capsys):
    code = github_drain_intake_cli.main(["finish all issues in project Widgets"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["execution_authorized"] is False
    assert payload["outcome"]["status"] == "BLOCKED"
    assert payload["outcome"]["execution_authorized"] is False
    assert payload["outcome"]["reason_code"] == "repository_missing"


def test_cli_success_is_still_planned_not_executed_nonzero(monkeypatch, tmp_path, capsys):
    from tests.test_github_drain_intake_integration import ReadOnlyGitHub

    source = ReadOnlyGitHub("acme/widgets", [])
    monkeypatch.setattr(github_drain_intake_cli, "GitHubSourceAdapter", lambda *_a, **_k: source)
    code = github_drain_intake_cli.main([
        "finish all issues in acme/widgets",
        "--workspace", str(tmp_path),
        "--checkpoint", str(tmp_path / "cli.json"),
        "--no-map",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["outcome"]["status"] == "PLANNED_NOT_EXECUTED"
    assert payload["outcome"]["execution_authorized"] is False
    assert source.effect_calls == []


def test_cli_adapter_failure_is_structured(monkeypatch, capsys):
    def boom(*_args, **_kwargs):
        raise RuntimeError("adapter unavailable")

    monkeypatch.setattr(github_drain_intake_cli, "GitHubSourceAdapter", boom)
    code = github_drain_intake_cli.main(["finish all issues in acme/widgets", "--no-map"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 4
    assert payload["execution_authorized"] is False
    assert payload["outcome"]["status"] == "FAILED"
    assert payload["outcome"]["execution_authorized"] is False
    assert payload["outcome"]["reason_code"] == "drain_intake_cli_failed"
