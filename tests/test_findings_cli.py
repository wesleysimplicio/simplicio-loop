"""WI-466 integration test for the `simplicio-loop findings` CLI subcommand."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simplicio_loop import cli as cli_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # Route findings store into tmp to avoid polluting the repo.
    import simplicio_loop.finding_router as rt

    sp = tmp_path / "issue_routes.json"
    monkeypatch.setattr(rt, "LOCAL_STORE", sp)
    monkeypatch.setattr(rt, "_gh_available", lambda: False)
    findings_dir = tmp_path / "findings"
    import simplicio_loop.finding_report as fr_mod

    monkeypatch.setattr(fr_mod, "_FINDINGS_DIR", findings_dir)
    return sp


class _Args:
    def __init__(self, sub, json_flag=False):
        self.findings_command = sub
        self.json = json_flag


def test_findings_doctor_reports_store_health():
    # Emit a real finding so the findings store exists, then assert the doctor
    # surfaces BOTH the findings store and the routes store (WI-466 consistency fix).
    import simplicio_loop.finding_report as fr_mod

    fr_mod.emit_finding("survey", "doc-1", "medium", "m.py:9", True)
    rc = cli_mod.findings_command(_Args("doctor", json_flag=True))
    assert rc == 0
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli_mod.findings_command(_Args("doctor", json_flag=True))
    payload = json.loads(buf.getvalue())
    assert payload["schema"] == "simplicio.finding-doctor/v1"
    assert payload["findings_store_present"] is True
    assert "findings_store_path" in payload
    assert "routes_store_path" in payload
    assert payload["router_importable"] is True


def test_findings_reconcile_empty():
    rc = cli_mod.findings_command(_Args("reconcile"))
    assert rc == 0


def test_findings_reconcile_blocks_when_untracked():
    import simplicio_loop.finding_router as rt

    # Route a finding with gh forced unavailable -> local fallback (untracked).
    rt.route_finding("operate", "blk-cli", "high", "cli.py:1", True, item_id="WI-466")
    rc = cli_mod.findings_command(_Args("reconcile", json_flag=True))
    assert rc == 1  # completion gate must block (non-zero exit)


def test_findings_reconcile_json_has_blocked_flag():
    import io
    import contextlib
    import simplicio_loop.finding_router as rt

    rt.route_finding("operate", "blk-json", "high", "cli.py:2", True, item_id="WI-466")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli_mod.findings_command(_Args("reconcile", json_flag=True))
    payload = json.loads(buf.getvalue())
    assert payload["completion_blocked"] is True
    assert payload["untracked_count"] >= 1


def test_findings_report_aggregates_after_route():
    import simplicio_loop.finding_router as rt

    rt.route_finding("operate", "reg-1", "high", "cli.py:1", True, item_id="WI-466")
    rc = cli_mod.findings_command(_Args("report", json_flag=True))
    assert rc == 0


def test_findings_list_after_emit():
    import simplicio_loop.finding_report as fr

    fr.emit_finding("survey", "d1", "medium", "m.py:9", True)
    rc = cli_mod.findings_command(_Args("list", json_flag=True))
    assert rc == 0


def test_findings_unknown_command_has_deterministic_error(capsys):
    rc = cli_mod.findings_command(_Args("unknown", json_flag=True))
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "error": {
            "code": "unknown_findings_command",
            "message": "unknown findings subcommand",
            "value": "unknown",
        },
        "ok": False,
        "schema": "simplicio.finding-command-error/v1",
    }


def test_ledger_replay_uses_live_cli_ledger_exports(monkeypatch, capsys):
    captured = {}

    class FakeLedger:
        def __init__(self, path, compatibility):
            captured["path"] = path
            captured["compatibility"] = compatibility

        def replay(self, recover_trailing):
            captured["recover_trailing"] = recover_trailing
            return [{"event_id": "from-cli-seam"}]

    monkeypatch.setattr(cli_mod, "EventLedger", FakeLedger)
    monkeypatch.setattr(cli_mod, "CONTEXT_SCHEMA", "test.context/v1")
    monkeypatch.setattr(cli_mod, "HANDSHAKE_SCHEMA", "test.handshake/v1")
    monkeypatch.setattr(cli_mod, "REQUIRED_CONTEXT_FIELDS", ("run_id",))

    assert cli_mod.ledger_replay("events.jsonl", True, True, "", "") == 0
    payload = json.loads(capsys.readouterr().out)
    assert captured == {
        "path": "events.jsonl", "compatibility": True, "recover_trailing": True,
    }
    assert payload["events"] == [{"event_id": "from-cli-seam"}]
    assert payload["context_schema"] == "test.context/v1"
    assert payload["required_context"] == ["run_id"]


def _import_args(payload, repo_map="", labels=None, dry_run=False, receipt=""):
    args = _Args("import", json_flag=True)
    args.path = str(payload)
    args.repo_map = str(repo_map) if repo_map else ""
    args.label = list(labels or [])
    args.dry_run = dry_run
    args.receipt = str(receipt) if receipt else ""
    return args


def _finding(source, finding_id="a"):
    return {"finding_id": finding_id, "stage": "test", "severity": "high", "source": str(source)}


def test_findings_import_validates_array_before_creating(tmp_path, capsys):
    payload = tmp_path / "bad.json"
    payload.write_text(json.dumps([{"finding_id": "a"}]), encoding="utf-8")
    assert cli_mod.findings_command(_import_args(payload)) == 2
    assert "requires non-empty" in capsys.readouterr().out


def test_findings_import_dry_run_resolves_repo_map(tmp_path, capsys):
    payload = tmp_path / "findings.json"
    payload.write_text(json.dumps([_finding(tmp_path / "x.py:1")]), encoding="utf-8")
    repo_map = tmp_path / "repos.json"
    repo_map.write_text(json.dumps({str(tmp_path): "acme/widgets"}), encoding="utf-8")
    assert cli_mod.findings_command(_import_args(payload, repo_map, ["bug"], True)) == 0
    assert json.loads(capsys.readouterr().out) == {"0": "https://github.com/acme/widgets/issues/dry-run-0"}


def test_findings_import_pre_resolves_every_repo_before_effects(tmp_path, monkeypatch, capsys):
    payload = tmp_path / "findings.json"
    payload.write_text(json.dumps([_finding(tmp_path / "one.py"), _finding(tmp_path.parent / "two.py", "b")]), encoding="utf-8")
    repo_map = tmp_path / "repos.json"
    repo_map.write_text(json.dumps({str(tmp_path): "acme/widgets"}), encoding="utf-8")
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        return type("Result", (), {"returncode": 1, "stdout": ""})()
    monkeypatch.setattr(cli_mod._inspection_cli.subprocess, "run", fake_run)
    assert cli_mod.findings_command(_import_args(payload, repo_map)) == 2
    assert not any(command[0] == "gh" for command in calls)
    assert "could not resolve repository" in capsys.readouterr().out


@pytest.mark.parametrize("remote", [
    "https://github.com.evil/acme/widgets.git",
    "https://github.com@evil.example/acme/widgets.git",
    "git@github.com.evil:acme/widgets.git",
    "https://github.com/acme/../widgets.git",
])
def test_findings_import_rejects_malicious_remote(remote):
    assert cli_mod._inspection_cli._github_repo_from_remote(remote) is None


def test_findings_import_rejects_invalid_repo_map_before_effects(tmp_path, monkeypatch, capsys):
    payload = tmp_path / "findings.json"
    payload.write_text(json.dumps([_finding(tmp_path / "x.py")]), encoding="utf-8")
    repo_map = tmp_path / "repos.json"
    repo_map.write_text(json.dumps({str(tmp_path): "acme/../widgets"}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(cli_mod._inspection_cli.subprocess, "run", lambda command, **kwargs: calls.append(command))
    assert cli_mod.findings_command(_import_args(payload, repo_map)) == 2
    assert calls == []
    assert "invalid GitHub repository mapping" in capsys.readouterr().out


def test_findings_import_partial_failure_receipt_makes_retry_idempotent(tmp_path, monkeypatch, capsys):
    payload = tmp_path / "findings.json"
    payload.write_text(json.dumps([_finding(tmp_path / "x.py"), _finding(tmp_path / "y.py", "b")]), encoding="utf-8")
    repo_map = tmp_path / "repos.json"
    repo_map.write_text(json.dumps({str(tmp_path): "acme/widgets"}), encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    creates = []
    def fail_second(command, **kwargs):
        if command[2] == "list":
            return type("Result", (), {"returncode": 0, "stdout": "[]"})()
        creates.append(command)
        if len(creates) == 2:
            return type("Result", (), {"returncode": 1, "stdout": ""})()
        return type("Result", (), {"returncode": 0, "stdout": json.dumps({"url": "https://github.com/acme/widgets/issues/1"})})()
    monkeypatch.setattr(cli_mod._inspection_cli.subprocess, "run", fail_second)
    args = _import_args(payload, repo_map, ["bug"], receipt=receipt)
    assert cli_mod.findings_command(args) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["urls"] == {"0": "https://github.com/acme/widgets/issues/1"}

    retry_creates = []
    def retry(command, **kwargs):
        if command[2] == "list":
            return type("Result", (), {"returncode": 0, "stdout": "[]"})()
        retry_creates.append(command)
        return type("Result", (), {"returncode": 0, "stdout": json.dumps({"url": "https://github.com/acme/widgets/issues/2"})})()
    monkeypatch.setattr(cli_mod._inspection_cli.subprocess, "run", retry)
    assert cli_mod.findings_command(args) == 0
    assert len(retry_creates) == 1
    assert json.loads(capsys.readouterr().out) == {"0": "https://github.com/acme/widgets/issues/1", "1": "https://github.com/acme/widgets/issues/2"}


def test_findings_import_crash_after_effect_reconciles_remote_marker(tmp_path, monkeypatch, capsys):
    payload = tmp_path / "findings.json"
    payload.write_text(json.dumps([_finding(tmp_path / "x.py")]), encoding="utf-8")
    repo_map = tmp_path / "repos.json"
    repo_map.write_text(json.dumps({str(tmp_path): "acme/widgets"}), encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    calls = []
    def remote(command, **kwargs):
        calls.append(command)
        if command[2] == "list":
            marker = command[command.index("--search") + 1]
            row = {"url": "https://github.com/acme/widgets/issues/9", "title": marker, "body": ""}
            return type("Result", (), {"returncode": 0, "stdout": json.dumps([row])})()
        raise AssertionError("create must not run after remote reconciliation")
    monkeypatch.setattr(cli_mod._inspection_cli.subprocess, "run", remote)
    assert cli_mod.findings_command(_import_args(payload, repo_map, receipt=receipt)) == 0
    assert all(command[2] == "list" for command in calls)
    assert json.loads(capsys.readouterr().out) == {"0": "https://github.com/acme/widgets/issues/9"}


def test_findings_import_write_failure_returns_created_url(tmp_path, monkeypatch, capsys):
    payload = tmp_path / "findings.json"
    payload.write_text(json.dumps([_finding(tmp_path / "x.py")]), encoding="utf-8")
    repo_map = tmp_path / "repos.json"
    repo_map.write_text(json.dumps({str(tmp_path): "acme/widgets"}), encoding="utf-8")
    def remote(command, **kwargs):
        data = [] if command[2] == "list" else {"url": "https://github.com/acme/widgets/issues/7"}
        return type("Result", (), {"returncode": 0, "stdout": json.dumps(data)})()
    monkeypatch.setattr(cli_mod._inspection_cli.subprocess, "run", remote)
    monkeypatch.setattr(cli_mod._inspection_cli, "_save_import_receipt", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    rc = cli_mod.findings_command(_import_args(payload, repo_map, receipt=tmp_path / "receipt.json"))
    result = json.loads(capsys.readouterr().out)
    assert rc == 1 and result["error"]["code"] == "receipt_write_failed"
    assert result["urls"] == {"0": "https://github.com/acme/widgets/issues/7"}


@pytest.mark.parametrize("receipt_value", [
    [],
    {"schema": "simplicio.findings-import-receipt/v1", "batch_id": "wrong", "urls": {}},
])
def test_findings_import_non_dict_or_mismatched_receipt_is_structured(tmp_path, capsys, receipt_value):
    payload = tmp_path / "findings.json"
    payload.write_text(json.dumps([_finding(tmp_path / "x.py")]), encoding="utf-8")
    repo_map = tmp_path / "repos.json"
    repo_map.write_text(json.dumps({str(tmp_path): "acme/widgets"}), encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(receipt_value), encoding="utf-8")
    assert cli_mod.findings_command(_import_args(payload, repo_map, receipt=receipt)) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "corrupt_import_receipt"


def test_findings_import_rejects_forged_receipt_indices_and_urls(tmp_path):
    mod = cli_mod._inspection_cli
    batch_id = "b" * 64
    receipt = tmp_path / "receipt.json"
    for urls in ({"1": "https://github.com/acme/widgets/issues/1"}, {"0": "https://github.com/evil/widgets/issues/1"}):
        receipt.write_text(json.dumps({"schema": mod._IMPORT_RECEIPT_SCHEMA, "batch_id": batch_id, "urls": urls}), encoding="utf-8")
        with pytest.raises(ValueError):
            mod._load_import_receipt(receipt, batch_id, ["acme/widgets"])


def test_findings_import_concurrent_calls_create_once(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    payload = tmp_path / "findings.json"
    payload.write_text(json.dumps([_finding(tmp_path / "x.py")]), encoding="utf-8")
    repo_map = tmp_path / "repos.json"
    repo_map.write_text(json.dumps({str(tmp_path): "acme/widgets"}), encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    creates = []
    guard = threading.Lock()
    def remote(command, **kwargs):
        if command[2] == "list":
            return type("Result", (), {"returncode": 0, "stdout": "[]"})()
        with guard:
            creates.append(command)
        return type("Result", (), {"returncode": 0, "stdout": json.dumps({"url": "https://github.com/acme/widgets/issues/1"})})()
    monkeypatch.setattr(cli_mod._inspection_cli.subprocess, "run", remote)
    args = _import_args(payload, repo_map, receipt=receipt)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: cli_mod.findings_command(args), range(2)))
    assert results == [0, 0]
    assert len(creates) == 1
