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
    monkeypatch.setattr(cli_mod._inspection_cli, "_marker_state_path", lambda root, finding_hash: tmp_path / "markers" / f"{finding_hash}.json")
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


def test_exact_marker_match_and_paginated_server_search(tmp_path, monkeypatch):
    mod = cli_mod._inspection_cli
    finding = _finding("x.py")
    marker = mod._marker("a" * 64)
    calls = []
    rows = [{"url": "https://github.com/acme/widgets/issues/1", "title": f"x [{marker}evil]", "body": "", "labels": [], "author": {"login": "me"}}]
    monkeypatch.setattr(mod.subprocess, "run", lambda command, **kwargs: (calls.append(command) or _result(rows)))
    assert mod._find_remote_issue("acme/widgets", finding, [], marker) == (True, None)
    assert calls[0][calls[0].index("--limit") + 1] == "1000"
    assert calls[0][calls[0].index("--author") + 1] == "@me"

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
    finding_hash = mod._finding_hash(finding, "acme/widgets", "x.py")
    marker = mod._marker(finding_hash)
    receipt = tmp_path / "r.json"
    receipt.write_text(json.dumps({"schema": mod._IMPORT_RECEIPT_SCHEMA, "entries": {marker: {"repo": "acme/widgets", "finding_hash": finding_hash, "url": "https://github.com/acme/widgets/issues/3"}}}), encoding="utf-8")
    calls = []

    def gh(command, **kwargs):
        calls.append(command)
        canonical = dict(finding, file="x.py")
        row = {"url": "https://github.com/acme/widgets/issues/3", "title": mod._issue_title(canonical, marker), "body": mod._issue_body(canonical, marker), "labels": [], "author": {"login": "me"}}
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
        if command[0] == "git":
            return _result("", returncode=1)
        marker = command[command.index("--search") + 1].split(chr(34))[1] if command[2] == "list" else ""
        if command[2] == "list":
            with guard:
                created = bool(creates)
            if not created:
                return _result([])
            canonical = dict(finding, file="x.py")
            return _result([{"url": "https://github.com/acme/widgets/issues/8", "title": mod._issue_title(canonical, marker), "body": mod._issue_body(canonical, marker), "labels": [], "author": {"login": "me"}}])
        with guard:
            creates.append(command)
        return _result("https://github.com/acme/widgets/issues/8\n")

    monkeypatch.setattr(mod.subprocess, "run", gh)
    args = [_import_args(path, mapping, receipt=tmp_path / "r1.json"), _import_args(path, mapping, receipt=tmp_path / "r2.json")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(cli_mod.findings_command, args))
    assert results == [0, 0] and len(creates) == 1


def test_remote_timeout_margin_prevents_takeover_past_base_lease(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time

    mod = cli_mod._inspection_cli
    finding = _finding(tmp_path / "x.py")
    created = threading.Event()
    creates = []
    observed_leases = []
    url = "https://github.com/acme/widgets/issues/19"
    monkeypatch.setattr(mod, "_MARKER_LEASE_SECONDS", 0.01)
    monkeypatch.setattr(mod, "_GH_TIMEOUT_SECONDS", 0.10)
    monkeypatch.setattr(mod, "_MARKER_LEASE_MARGIN_SECONDS", 0.05)

    def find_remote(repo, canonical, labels, marker):
        return (True, url) if created.is_set() else (True, None)

    def create_remote(repo, canonical, labels, marker):
        state_path = mod._marker_state_path(tmp_path, mod._finding_hash(canonical, repo, "x.py"))
        state = mod._read_marker_state(state_path)
        observed_leases.append(state["expires"] - state["created"])
        creates.append(marker)
        time.sleep(0.08)
        created.set()
        return url

    monkeypatch.setattr(mod, "_find_remote_issue", find_remote)
    monkeypatch.setattr(mod, "_create_remote_issue", create_remote)

    def coordinate(delay):
        time.sleep(delay)
        return mod._coordinate_finding(finding, "acme/widgets", [], tmp_path, "x.py")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(coordinate, (0.0, 0.03)))
    assert len(creates) == 1
    assert observed_leases[0] == pytest.approx(0.15)
    assert all(result[2] == url and result[3] is None for result in results)


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
    assert mod._find_remote_issue("acme/widgets", finding, [], "marker") == (False, None)
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


def test_marker_state_is_filesystem_safe_durable_and_detects_corruption(tmp_path):
    mod = cli_mod._inspection_cli
    finding_hash = "a" * 64
    path = mod._marker_state_path(tmp_path, finding_hash)
    assert path.name == f"{finding_hash}.json" and ":" not in path.name
    kind, claim = mod._claim_marker(path, "acme/widgets", finding_hash, "owner-1")
    assert kind == "owner"
    assert {"schema", "owner", "pid", "fence", "created", "expires", "digest"}.issubset(claim)
    assert mod._read_marker_state(path) == claim
    value = json.loads(path.read_text(encoding="utf-8"))
    value["owner"] = "tampered"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(mod._MarkerStateError, match="digest"):
        mod._read_marker_state(path)


def test_stale_claim_takeover_and_failed_owner_release(tmp_path, monkeypatch):
    mod = cli_mod._inspection_cli
    path = tmp_path / "marker.json"
    finding_hash = "b" * 64
    monkeypatch.setattr(mod, "_MARKER_LEASE_SECONDS", 0.01)
    kind, first = mod._claim_marker(path, "acme/widgets", finding_hash, "crashed-owner")
    assert kind == "owner"
    monkeypatch.setattr(mod.time, "time", lambda: first["expires"] + 1)
    kind, second = mod._claim_marker(path, "acme/widgets", finding_hash, "replacement")
    assert kind == "owner" and second["fence"] != first["fence"]
    assert mod._finish_marker(path, second, "acme/widgets", finding_hash, None) is True
    kind, third = mod._claim_marker(path, "acme/widgets", finding_hash, "retry")
    assert kind == "owner" and third["owner"] == "retry"


def test_canonical_identity_across_worktrees_separators_and_case(tmp_path):
    mod = cli_mod._inspection_cli
    root_a = tmp_path / "WorkTree-A"
    root_b = tmp_path / "worktree-b"
    file_a = root_a / "Src" / "Thing.PY"
    file_b = root_b / "src" / "thing.py"
    canonical_a = mod._canonical_file(str(file_a), root_a)
    canonical_b = mod._canonical_file(str(file_b).replace("\\", "/"), root_b)
    finding_a = _finding(file_a)
    finding_b = _finding(file_b)
    assert canonical_a == canonical_b == "src/thing.py"
    assert mod._finding_hash(finding_a, "Acme/Widgets", canonical_a) == mod._finding_hash(finding_b, "acme/widgets", canonical_b)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows .cmd executable contract")
def test_fake_gh_cmd_executable_e2e_on_windows(tmp_path, monkeypatch):
    import os

    fake = tmp_path / "fake-bin"
    fake.mkdir()
    (fake / "gh.py").write_text("print('fake-gh-ok')\n", encoding="utf-8")
    (fake / "gh.cmd").write_text(f'@"{sys.executable}" "%~dp0gh.py" %*\n', encoding="utf-8")
    monkeypatch.setenv("PATH", str(fake) + os.pathsep + os.environ["PATH"])
    result = cli_mod._inspection_cli._run_gh(["gh", "--version"])
    assert result.returncode == 0 and result.stdout.strip() == "fake-gh-ok"


def test_cross_process_different_cwd_slow_owner_shares_claim(tmp_path):
    import os

    coordination = tmp_path / "common-git"
    cwd_a = tmp_path / "cwd-a"
    cwd_b = tmp_path / "cwd-b"
    cwd_a.mkdir()
    cwd_b.mkdir()
    runner = tmp_path / "claim-runner.py"
    runner.write_text(
        "import json,os,sys,time\nfrom pathlib import Path\nfrom simplicio_loop import inspection_cli as m\nos.chdir(sys.argv[2])\nh='c'*64; p=m._marker_state_path(Path(sys.argv[1]),h); kind,state=m._claim_marker(p,'acme/widgets',h,str(os.getpid()))\nif kind=='owner':\n time.sleep(0.4); m._finish_marker(p,state,'acme/widgets',h,'https://github.com/acme/widgets/issues/88'); state=m._read_marker_state(p)\nelse:\n end=time.time()+5\n while time.time()<end:\n  state=m._read_marker_state(p)\n  if state and state.get('status')=='resolved': break\n  time.sleep(0.05)\nPath(sys.argv[3]).write_text(json.dumps(state),encoding='utf-8')\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    outputs = [tmp_path / f"claim-{index}.json" for index in range(2)]
    pids = [os.spawnve(os.P_NOWAIT, sys.executable, [sys.executable, str(runner), str(coordination), str(cwd), str(output)], env) for cwd, output in zip((cwd_a, cwd_b), outputs)]
    statuses = [os.waitpid(pid, 0)[1] for pid in pids]
    states = [json.loads(output.read_text(encoding="utf-8")) for output in outputs]
    assert statuses == [0, 0]
    assert all(state["status"] == "resolved" and state["url"].endswith("/88") for state in states)
