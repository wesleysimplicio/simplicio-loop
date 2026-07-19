"""Integration tests: the quality-matrix gate wired into the real completion oracle (#278).

These exercise `simplicio_loop.oracle.evaluate_completion` end-to-end against the run/loop
artifact contract (not the module in isolation, and not through the CLI subprocess boundary —
that is the system-level test in test_quality_matrix_system.py).
"""
import json
import sys

from simplicio_loop.oracle import evaluate_completion
from simplicio_loop.quality_matrix import REQUIRED_REQUIREMENTS


def _passing_quality_matrix(**overrides):
    receipt = {
        "schema": "simplicio.quality-matrix/v1",
        "coverage_threshold": 85,
        "requirements": {
            name: {"status": "pass", "proof_ref": f"tests/{name}"} for name in REQUIRED_REQUIREMENTS
        },
        "coverage": {"measured": 91.2},
    }
    receipt.update(overrides)
    return receipt


def _seed_loop(loop):
    (loop / "scratchpad.md").write_text("---\ncompletion_promise: \"DONE\"\n---\ngoal\n", encoding="utf-8")
    (loop / "anchor.json").write_text(json.dumps({"criteria": [{"id": "AC1", "status": "done"}]}), encoding="utf-8")
    (loop / "watcher_challenge.json").write_text(json.dumps({
        "challenge": "abc", "goal_fp": "", "written_at": "2026-07-10T00:00:00Z"
    }), encoding="utf-8")
    (loop / "watcher_state.json").write_text(json.dumps({
        "match": True, "status": "MEASURED", "checked_at": "2026-07-10T00:00:01Z",
        "challenge": "abc", "goal_fp": ""
    }), encoding="utf-8")


def _seed_run(run_dir, *, with_quality_matrix=True, quality_overrides=None):
    (run_dir / "manifest.json").write_text(json.dumps({
        "schema": "simplicio.run-manifest/v1", "delivery_target": "verified"
    }), encoding="utf-8")
    (run_dir / "task-contract.json").write_text(json.dumps({"schema": "simplicio.task-contract-collection/v1"}), encoding="utf-8")
    (run_dir / "mapper-context.json").write_text(json.dumps({"handoff": {}}), encoding="utf-8")
    (run_dir / "operator-receipt.json").write_text(json.dumps({"schema": "simplicio.operator-receipt/v0"}), encoding="utf-8")
    (run_dir / "evidence-receipt.json").write_text(json.dumps({
        "schema": "simplicio.evidence-receipt/v1", "status": "VERIFIED",
        "criteria": [{"id": "AC1", "verification_state": "verified"}],
        "summary": {"criteria_total": 1, "criteria_verified": 1,
                    "scenario_total": 1, "scenario_verified": 1, "rule_total": 1, "rule_verified": 1}
    }), encoding="utf-8")
    (run_dir / "delivery-receipt.json").write_text(json.dumps({
        "schema": "simplicio.delivery-receipt/v1", "target": "verified", "current_state": "verified",
        "ready": True, "source_kind": "local",
        "source_payload": {"evidence_receipt": "evidence-receipt.json", "criteria_verified": 1}
    }), encoding="utf-8")
    if with_quality_matrix:
        (run_dir / "quality-matrix.json").write_text(
            json.dumps(_passing_quality_matrix(**(quality_overrides or {}))), encoding="utf-8")


def test_completion_blocked_when_quality_matrix_receipt_is_entirely_absent(tmp_path):
    loop = tmp_path / "loop"; loop.mkdir()
    run_dir = tmp_path / "run"; run_dir.mkdir()
    _seed_loop(loop)
    _seed_run(run_dir, with_quality_matrix=False)
    result = evaluate_completion(str(loop), str(run_dir), response_text="<promise>DONE</promise>")
    assert result["ready"] is False
    assert result["reason_code"] == "quality_matrix_missing"
    assert result["verdict"] != "COMPLETE"


def test_completion_blocked_when_one_lane_is_missing(tmp_path):
    loop = tmp_path / "loop"; loop.mkdir()
    run_dir = tmp_path / "run"; run_dir.mkdir()
    _seed_loop(loop)
    quality = _passing_quality_matrix()
    del quality["requirements"]["benchmark"]
    _seed_run(run_dir)
    (run_dir / "quality-matrix.json").write_text(json.dumps(quality), encoding="utf-8")
    result = evaluate_completion(str(loop), str(run_dir), response_text="<promise>DONE</promise>")
    assert result["ready"] is False
    assert result["reason_code"] == "quality_benchmark_missing"


def test_completion_blocked_when_coverage_below_configured_minimum(tmp_path):
    loop = tmp_path / "loop"; loop.mkdir()
    run_dir = tmp_path / "run"; run_dir.mkdir()
    _seed_loop(loop)
    _seed_run(run_dir, quality_overrides={"coverage": {"measured": 60.0}})
    result = evaluate_completion(str(loop), str(run_dir), response_text="<promise>DONE</promise>")
    assert result["ready"] is False
    assert result["reason_code"] == "coverage_below_threshold"
    assert result["coverage_measured"] == 60.0
    assert result["coverage_threshold"] == 85.0


def test_completion_succeeds_only_once_full_quality_matrix_and_coverage_pass(tmp_path):
    loop = tmp_path / "loop"; loop.mkdir()
    run_dir = tmp_path / "run"; run_dir.mkdir()
    _seed_loop(loop)
    _seed_run(run_dir)
    result = evaluate_completion(str(loop), str(run_dir), response_text="<promise>DONE</promise>")
    assert result["ready"] is True
    assert result["verdict"] == "COMPLETE"
    assert result["coverage_measured"] == 91.2
    assert result["coverage_threshold"] == 85.0
    quality_gates = [g for g in result["gates"] if g["name"] == "quality_matrix"]
    assert quality_gates and quality_gates[0]["status"] == "pass"


if __name__ == "__main__":
    sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_quality_matrix_integration")
