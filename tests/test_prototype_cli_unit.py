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


def test_validate_alias_matches_validate_schema(tmp_path, capsys):
    plan = pg.build_plan(work_item_id="wi-1", goal="g", prototype_type="schema", source_sha="abc")
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    rc, out = _run(["validate", "--file", str(plan_file), "--current-source-sha", "abc"], capsys)
    assert rc == 0
    assert out["valid"] is True


def _plan_file(tmp_path, work_item="wi-gen", **overrides):
    kwargs = {"work_item_id": work_item, "goal": "g", "prototype_type": "schema", "source_sha": "abc",
              "level": "P0", "validators": ["check_a"]}
    kwargs.update(overrides)
    plan = pg.build_plan(**kwargs)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return plan, path


def test_generate_dispatches_candidates_and_persists(tmp_path, capsys):
    _, plan_path = _plan_file(tmp_path)
    candidates_path = tmp_path / "candidates_spec.json"
    candidates_path.write_text(json.dumps([
        {"candidate_id": "c1", "commands": [["python3", "-c", "print(1)"]], "strategy": "s", "agent_id": "a"},
        {"candidate_id": "c2", "commands": [["python3", "-c", "import sys; sys.exit(1)"]], "strategy": "s2", "agent_id": "a2"},
    ]), encoding="utf-8")
    rc, out = _run([
        "generate", "--plan-file", str(plan_path), "--candidates-file", str(candidates_path),
        "--work-item", "wi-gen", "--persist", "--repo", str(tmp_path),
    ], capsys)
    assert rc == 0
    assert out["report"]["by_status"] == {"ok": 1, "failed": 1}
    statuses = {c["candidate_id"]: c["status"] for c in out["candidates"]}
    assert statuses == {"c1": "validated", "c2": "rejected"}
    assert out["candidates_path"].endswith("wi-gen.candidates.json")


def test_generate_requires_work_item_to_persist(tmp_path, capsys):
    _, plan_path = _plan_file(tmp_path)
    candidates_path = tmp_path / "candidates_spec.json"
    candidates_path.write_text(json.dumps([
        {"candidate_id": "c1", "commands": [["python3", "-c", "print(1)"]]},
    ]), encoding="utf-8")
    rc, out = _run([
        "generate", "--plan-file", str(plan_path), "--candidates-file", str(candidates_path), "--persist",
    ], capsys)
    assert rc == 2
    assert "error" in out


def _candidate(plan, candidate_id, *, evidence=False, validated=True):
    return pg.build_candidate(
        plan=plan, candidate_id=candidate_id, strategy="s", agent_id=f"agent-{candidate_id}",
        artifact_hash=f"hash-{candidate_id}",
        validation_results=[{"validator": "check_a", "passed": validated}],
        evidence_refs=["evidence.txt"] if evidence else [],
        status="validated" if validated else "rejected",
    )


def test_compare_ranks_without_mutating_state(tmp_path, capsys):
    plan, plan_path = _plan_file(tmp_path, work_item="wi-cmp")
    winner = _candidate(plan, "c1", evidence=True)
    loser = _candidate(plan, "c2", evidence=False)
    candidates_path = tmp_path / "candidates.json"
    candidates_path.write_text(json.dumps([winner, loser]), encoding="utf-8")
    rc, out = _run([
        "compare", "--plan-file", str(plan_path), "--candidates-file", str(candidates_path),
        "--judge-id", "judge-1",
    ], capsys)
    assert rc == 0
    assert out["verdicts"][0]["candidate_id"] == "c1"
    assert out["verdicts"][0]["eligible_for_accept"] is True
    assert pg.load_state("wi-cmp", repo=str(tmp_path)) is None  # read-only: nothing persisted


def test_decide_produces_accept_without_persisting_state(tmp_path, capsys):
    plan, plan_path = _plan_file(tmp_path, work_item="wi-dec")
    winner = _candidate(plan, "c1", evidence=True)
    candidates_path = tmp_path / "candidates.json"
    candidates_path.write_text(json.dumps([winner]), encoding="utf-8")
    rc, out = _run([
        "decide", "--plan-file", str(plan_path), "--candidates-file", str(candidates_path),
        "--judge-id", "judge-1",
    ], capsys)
    assert rc == 0
    assert out["decision"]["decision"] == "ACCEPT"
    assert pg.load_state("wi-dec", repo=str(tmp_path)) is None


def test_promote_applies_judged_decision_to_persisted_state(tmp_path, capsys):
    plan, plan_path = _plan_file(tmp_path, work_item="wi-prom", level="P0")
    _run(["plan", "--work-item", "wi-prom", "--goal", "g", "--type", "schema",
          "--source-sha", "abc", "--level", "P0", "--validator", "check_a",
          "--repo", str(tmp_path)], capsys)
    winner = _candidate(plan, "c1", evidence=True)
    candidates_path = tmp_path / "candidates.json"
    candidates_path.write_text(json.dumps([winner]), encoding="utf-8")
    rc, out = _run([
        "promote", "--work-item", "wi-prom", "--plan-file", str(plan_path),
        "--candidates-file", str(candidates_path), "--judge-id", "judge-1", "--repo", str(tmp_path),
    ], capsys)
    assert rc == 0
    assert out["decision"]["decision"] == "ACCEPT"
    assert out["state"]["current_level"] == "P1"
    persisted = pg.load_state("wi-prom", repo=str(tmp_path))
    assert persisted["current_level"] == "P1"


def test_promote_requires_an_existing_tracked_state(tmp_path, capsys):
    plan, plan_path = _plan_file(tmp_path, work_item="wi-untracked")
    winner = _candidate(plan, "c1", evidence=True)
    candidates_path = tmp_path / "candidates.json"
    candidates_path.write_text(json.dumps([winner]), encoding="utf-8")
    rc, out = _run([
        "promote", "--work-item", "wi-untracked", "--plan-file", str(plan_path),
        "--candidates-file", str(candidates_path), "--judge-id", "judge-1", "--repo", str(tmp_path),
    ], capsys)
    assert rc == 2
    assert "error" in out


def test_reject_applies_manual_terminal_decision(tmp_path, capsys):
    plan, plan_path = _plan_file(tmp_path, work_item="wi-rej")
    _run(["plan", "--work-item", "wi-rej", "--goal", "g", "--type", "schema",
          "--source-sha", "abc", "--validator", "check_a", "--repo", str(tmp_path)], capsys)
    rc, out = _run([
        "reject", "--work-item", "wi-rej", "--plan-file", str(plan_path),
        "--candidate-hash", "deadbeef", "--reason", "manual override", "--repo", str(tmp_path),
    ], capsys)
    assert rc == 0
    assert out["state"]["status"] == "rejected"
    assert pg.load_state("wi-rej", repo=str(tmp_path))["status"] == "rejected"


def test_list_reports_tracked_items(tmp_path, capsys):
    _run(["plan", "--work-item", "wi-list", "--goal", "g", "--type", "schema",
          "--source-sha", "abc", "--repo", str(tmp_path)], capsys)
    rc, out = _run(["list", "--repo", str(tmp_path)], capsys)
    assert rc == 0
    assert any(item["work_item_id"] == "wi-list" and item["tracked"] is True for item in out["items"])


def test_show_reports_state_and_gate_for_tracked_item(tmp_path, capsys):
    _run(["plan", "--work-item", "wi-show", "--goal", "g", "--type", "schema",
          "--source-sha", "abc", "--repo", str(tmp_path)], capsys)
    rc, out = _run(["show", "--work-item", "wi-show", "--repo", str(tmp_path)], capsys)
    assert rc == 0
    assert out["state"]["work_item_id"] == "wi-show"
    assert out["gate"]["tracked"] is True


def test_show_reports_error_for_untracked_item(tmp_path, capsys):
    rc, out = _run(["show", "--work-item", "wi-missing", "--repo", str(tmp_path)], capsys)
    assert rc == 1
    assert "error" in out
