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
