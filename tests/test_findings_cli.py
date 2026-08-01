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
    monkeypatch.setattr(cli_mod._inspection_cli, "_marker_state_path", lambda marker: tmp_path / "markers" / (marker.rsplit(":", 1)[-1] + ".json"))
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


def _finding(source, finding_id="a", **extra):
    finding = {"file": str(source), "line": 7, "summary": f"summary-{finding_id}", "failure_scenario": f"failure-{finding_id}"}
    finding.update(extra)
    return finding


def _result(payload, returncode=0):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return type("Result", (), {"returncode": returncode, "stdout": text})()


def test_findings_import_validates_canonical_input(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps([{"file": "x"}]), encoding="utf-8")
    assert cli_mod.findings_command(_import_args(path)) == 2
    assert "requires file, line, summary" in capsys.readouterr().out


def test_findings_import_dry_run_and_internal_extra_fields(tmp_path, capsys):
    path = tmp_path / "f.json"
    path.write_text(json.dumps([_finding(tmp_path / "x.py", internal_id="ok")]), encoding="utf-8")
    mapping = tmp_path / "m.json"
    mapping.write_text(json.dumps({str(tmp_path): "acme/widgets"}), encoding="utf-8")
    assert cli_mod.findings_command(_import_args(path, mapping, dry_run=True)) == 0
    assert json.loads(capsys.readouterr().out)["0"].endswith("dry-run-0")


def test_findings_create_uses_real_gh_argv_and_plain_stdout(tmp_path, monkeypatch, capsys):
    path = tmp_path / "f.json"
    path.write_text(json.dumps([_finding(tmp_path / "x.py")]), encoding="utf-8")
    mapping = tmp_path / "m.json"
    mapping.write_text(json.dumps({str(tmp_path): "acme/widgets"}), encoding="utf-8")
    calls = []

    def gh(command, **kwargs):
        calls.append(command)
        return _result([]) if command[2] == "list" else _result("https://github.com/acme/widgets/issues/12\n")

    monkeypatch.setattr(cli_mod._inspection_cli.subprocess, "run", gh)
    assert cli_mod.findings_command(_import_args(path, mapping, ["bug"], receipt=tmp_path / "r.json")) == 0
    create = next(c for c in calls if c[2] == "create")
    assert "--json" not in create and create[-2:] == ["--label", "bug"]
    assert json.loads(capsys.readouterr().out) == {"0": "https://github.com/acme/widgets/issues/12"}


def test_marker_stable_across_order_append_and_labels(tmp_path):
    mod = cli_mod._inspection_cli
    a = _finding(tmp_path / "a.py")
    b = _finding(tmp_path / "b.py", "b")
    marker = mod._marker(mod._finding_hash(a, "acme/widgets"))
    assert marker == mod._marker(mod._finding_hash([b, a][1], "acme/widgets"))
    assert marker == mod._marker(mod._finding_hash((a, b, _finding(tmp_path / "c.py"))[0], "acme/widgets"))


def test_exact_marker_match_and_unbounded_list(tmp_path, monkeypatch):
    mod = cli_mod._inspection_cli
    marker = mod._marker("a" * 64)
    calls = []
    rows = [{"url": "https://github.com/acme/widgets/issues/1", "title": f"x [{marker}evil]", "body": ""}]
    monkeypatch.setattr(mod.subprocess, "run", lambda command, **kwargs: (calls.append(command) or _result(rows)))
    assert mod._find_remote_issue("acme/widgets", marker) == (True, None)
    assert calls[0][calls[0].index("--limit") + 1] == "1000000"


def test_receipt_provenance_and_non_dict_rejected(tmp_path):
    mod = cli_mod._inspection_cli
    path = tmp_path / "r.json"
    for value in ([], {"schema": mod._IMPORT_RECEIPT_SCHEMA, "entries": {"bad": {}}}):
        path.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(ValueError):
            mod._load_import_receipt(path)


def test_receipt_url_reverified_remotely(tmp_path, monkeypatch, capsys):
    finding = _finding(tmp_path / "x.py")
    path = tmp_path / "f.json"
    path.write_text(json.dumps([finding]), encoding="utf-8")
    mapping = tmp_path / "m.json"
    mapping.write_text(json.dumps({str(tmp_path): "acme/widgets"}), encoding="utf-8")
    mod = cli_mod._inspection_cli
    finding_hash = mod._finding_hash(finding, "acme/widgets")
    marker = mod._marker(finding_hash)
    receipt = tmp_path / "r.json"
    receipt.write_text(json.dumps({"schema": mod._IMPORT_RECEIPT_SCHEMA, "entries": {marker: {"repo": "acme/widgets", "finding_hash": finding_hash, "url": "https://github.com/acme/widgets/issues/3"}}}), encoding="utf-8")
    calls = []

    def gh(command, **kwargs):
        calls.append(command)
        row = {"url": "https://github.com/acme/widgets/issues/3", "title": f"x [{marker}]", "body": ""}
        return _result([row])

    monkeypatch.setattr(mod.subprocess, "run", gh)
    assert cli_mod.findings_command(_import_args(path, mapping, receipt=receipt)) == 0
    assert any(c[2] == "list" for c in calls) and json.loads(capsys.readouterr().out)["0"].endswith("/3")


def test_cross_receipt_concurrency_creates_once(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    finding = _finding(tmp_path / "x.py")
    path = tmp_path / "f.json"
    path.write_text(json.dumps([finding]), encoding="utf-8")
    mapping = tmp_path / "m.json"
    mapping.write_text(json.dumps({str(tmp_path): "acme/widgets"}), encoding="utf-8")
    creates = []
    guard = threading.Lock()
    mod = cli_mod._inspection_cli

    def gh(command, **kwargs):
        if command[2] == "list":
            with guard:
                created = bool(creates)
            if not created:
                return _result([])
            marker = command[command.index("--search") + 1]
            return _result([{"url": "https://github.com/acme/widgets/issues/8", "title": f"x [{marker}]", "body": ""}])
        with guard:
            creates.append(command)
        return _result("https://github.com/acme/widgets/issues/8\n")

    monkeypatch.setattr(mod.subprocess, "run", gh)
    args = [_import_args(path, mapping, receipt=tmp_path / "r1.json"), _import_args(path, mapping, receipt=tmp_path / "r2.json")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(cli_mod.findings_command, args))
    assert results == [0, 0] and len(creates) == 1


def test_remote_parsing_and_source_resolution_branches(tmp_path, monkeypatch):
    mod = cli_mod._inspection_cli
    assert mod._github_repo_from_remote("git@github.com:acme/widgets.git") == "acme/widgets"
    assert mod._github_repo_from_remote("ssh://github.com/acme/widgets.git") == "acme/widgets"
    assert mod._github_repo_from_remote("https://example.com/acme/widgets") is None
    monkeypatch.chdir(tmp_path)
    assert mod._source_parent("relative.py") == tmp_path
    with pytest.raises(ValueError, match="invalid GitHub repository mapping"):
        mod._repo_for_source(str(tmp_path / "x.py"), {str(tmp_path): "bad repo"})
    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("git absent")))
    assert mod._repo_for_source(str(tmp_path / "x.py"), {}) is None


def test_findings_loader_rejects_malformed_canonical_values(tmp_path):
    mod = cli_mod._inspection_cli
    path = tmp_path / "findings.json"
    cases = [
        {"not": "a list"},
        ["not an object"],
        [_finding(tmp_path / "x.py", line=0)],
        [_finding(tmp_path / "x.py", verdict="")],
    ]
    for payload in cases:
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError):
            mod._load_findings_import(path)
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid findings file"):
        mod._load_findings_import(path)


def test_remote_command_failure_branches(tmp_path, monkeypatch):
    mod = cli_mod._inspection_cli
    finding = _finding(tmp_path / "x.py")
    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("gh absent")))
    assert mod._find_remote_issue("acme/widgets", "marker") == (False, None)
    assert mod._create_remote_issue("acme/widgets", finding, [], "marker") is None


def test_import_reports_corrupt_receipt_and_coordinate_failure(tmp_path, monkeypatch, capsys):
    mod = cli_mod._inspection_cli
    path = tmp_path / "f.json"
    path.write_text(json.dumps([_finding(tmp_path / "x.py")]), encoding="utf-8")
    mapping = tmp_path / "m.json"
    mapping.write_text(json.dumps({str(tmp_path): "acme/widgets"}), encoding="utf-8")
    receipt = tmp_path / "r.json"
    receipt.write_text("[]", encoding="utf-8")
    assert cli_mod.findings_command(_import_args(path, mapping, receipt=receipt)) == 2
    assert "corrupt_import_receipt" in capsys.readouterr().out
    receipt.unlink()
    monkeypatch.setattr(mod, "_coordinate_finding", lambda *args: ("m", "h", None, "issue_create_failed"))
    assert cli_mod.findings_command(_import_args(path, mapping, receipt=receipt)) == 1
    assert "issue_create_failed" in capsys.readouterr().out
