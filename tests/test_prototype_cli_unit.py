"""Unit coverage for the `simplicio_loop.prototype_cli` argparse layer (#568 P0 slice).

Every verb here only calls functions already exported by `simplicio_loop.prototype_gate` --
this file proves the CLI wires flags to those functions correctly and propagates exit codes,
not new schema/state logic (that is `tests/test_prototype_gate.py`'s job).
"""
from __future__ import annotations

import json

import pytest

from simplicio_loop import prototype_cli as pcli
from simplicio_loop import prototype_gate as pg


def _run(argv, capsys):
    rc = pcli.main(argv)
    out = json.loads(capsys.readouterr().out)
    return rc, out


def test_plan_builds_and_persists_state(tmp_path, capsys):
    rc, out = _run([
        "plan", "--work-item", "wi-1", "--goal", "g", "--type", "schema",
        "--source-sha", "abc", "--level", "P0", "--repo", str(tmp_path),
    ], capsys)
    assert rc == 0
    assert out["plan"]["schema"] == pg.PLAN_SCHEMA
    assert out["state"]["current_level"] == "P0"
    assert out["state_path"].endswith("wi-1.json")
    assert pg.load_state("wi-1", repo=str(tmp_path)) == out["state"]


def test_plan_no_persist_skips_the_state_file(tmp_path, capsys):
    rc, out = _run([
        "plan", "--work-item", "wi-2", "--goal", "g", "--type", "schema",
        "--source-sha", "abc", "--repo", str(tmp_path), "--no-persist",
    ], capsys)
    assert rc == 0
    assert out["state_path"] is None
    assert pg.load_state("wi-2", repo=str(tmp_path)) is None


def test_plan_rejects_unknown_prototype_type():
    with pytest.raises(SystemExit):
        pcli.main([
            "plan", "--work-item", "wi-1", "--goal", "g", "--type", "not-a-type",
            "--source-sha", "abc",
        ])


def test_classify_reports_required_level(capsys):
    rc, out = _run(["classify", "--task-description", "t", "--signal", "security"], capsys)
    assert rc == 0
    assert out["required"] is True
    assert out["level"] == "FULL"


def test_classify_exit_code_flag_signals_not_required(capsys):
    rc, out = _run(["classify", "--task-description", "typo fix", "--exit-code"], capsys)
    assert rc == 1
    assert out["required"] is False


def test_classify_emit_not_required_receipt_needs_work_item(capsys):
    rc, out = _run(["classify", "--task-description", "typo fix",
                    "--emit-not-required-receipt"], capsys)
    assert rc == 2
    assert "error" in out


def test_classify_emit_not_required_receipt(capsys):
    rc, out = _run(["classify", "--task-description", "typo fix",
                    "--emit-not-required-receipt", "--work-item", "wi-3"], capsys)
    assert rc == 0
    assert out["not_required_receipt"]["schema"] == pg.NOT_REQUIRED_SCHEMA


def test_validate_schema_valid_plan(tmp_path, capsys):
    plan = pg.build_plan(work_item_id="wi-1", goal="g", prototype_type="schema", source_sha="abc")
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    rc, out = _run(["validate-schema", "--file", str(plan_file), "--current-source-sha", "abc"], capsys)
    assert rc == 0
    assert out["valid"] is True


def test_validate_schema_drifted_plan_exits_nonzero(tmp_path, capsys):
    plan = pg.build_plan(work_item_id="wi-1", goal="g", prototype_type="schema", source_sha="abc")
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    rc, out = _run(["validate-schema", "--file", str(plan_file), "--current-source-sha", "changed"], capsys)
    assert rc == 1
    assert out["valid"] is False


def test_validate_schema_unknown_schema_is_rejected(tmp_path, capsys):
    bogus = tmp_path / "bogus.json"
    bogus.write_text(json.dumps({"schema": "not-a-real-schema"}), encoding="utf-8")
    rc, out = _run(["validate-schema", "--file", str(bogus)], capsys)
    assert rc == 2
    assert out["valid"] is False


def test_doctor_reports_schemas_and_tracked_items(tmp_path, capsys):
    pcli.main(["plan", "--work-item", "wi-doc", "--goal", "g", "--type", "schema",
              "--source-sha", "abc", "--repo", str(tmp_path)])
    capsys.readouterr()  # discard the plan verb's own output
    rc, out = _run(["doctor", "--repo", str(tmp_path)], capsys)
    assert rc == 0
    assert pg.PLAN_SCHEMA in out["schemas"]
    assert out["stall_detector_available"] is True
    assert any(item["work_item_id"] == "wi-doc" for item in out["tracked_items"])
