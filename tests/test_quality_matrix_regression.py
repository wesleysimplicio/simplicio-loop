"""Regression tests (#278): a delivery must never be closeable with zero, or partial, evidence.

These pin down the exact bug class the issue calls out — "tentativas de fechamento sem
qualquer evidencia obrigatoria" — at the oracle level (the same function `simplicio_loop.cli`'s
`oracle` command and `scripts/completion_oracle.py` both delegate to). If a future change
accidentally makes `evaluate_completion` ready-by-default, or lets any one quality lane be
skipped, these fail.
"""
import json
import sys

from simplicio_loop.oracle import evaluate_completion
from simplicio_loop.quality_matrix import REQUIRED_REQUIREMENTS, evaluate_quality_matrix


def _seed_loop(loop):
    (loop / "scratchpad.md").write_text("---\ncompletion_promise: \"DONE\"\n---\ngoal\n", encoding="utf-8")
    (loop / "anchor.json").write_text(json.dumps({"criteria": [{"id": "AC1", "status": "done"}]}), encoding="utf-8")
    (loop / "watcher_challenge.json").write_text(json.dumps({
        "challenge": "abc", "written_at": "2026-07-10T00:00:00Z"
    }), encoding="utf-8")
    (loop / "watcher_state.json").write_text(json.dumps({
        "match": True, "status": "MEASURED", "checked_at": "2026-07-10T00:00:01Z", "challenge": "abc"
    }), encoding="utf-8")


def _seed_run_without_quality_matrix(run_dir):
    (run_dir / "manifest.json").write_text(json.dumps({"delivery_target": "verified"}), encoding="utf-8")
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


def test_zero_evidence_close_attempt_is_rejected(tmp_path):
    """The exact regression scenario named in issue #278: a run with every other gate
    satisfied but NO quality-matrix receipt at all must never be reported COMPLETE."""
    loop = tmp_path / "loop"; loop.mkdir()
    run_dir = tmp_path / "run"; run_dir.mkdir()
    _seed_loop(loop)
    _seed_run_without_quality_matrix(run_dir)
    result = evaluate_completion(str(loop), str(run_dir), response_text="<promise>DONE</promise>")
    assert result["ready"] is False
    assert result["verdict"] != "COMPLETE"
    assert result["reason_code"] == "quality_matrix_missing"


def test_empty_requirements_object_is_rejected_not_treated_as_satisfied(tmp_path):
    """An empty `requirements: {}` must not be interpreted as 'nothing required'."""
    loop = tmp_path / "loop"; loop.mkdir()
    run_dir = tmp_path / "run"; run_dir.mkdir()
    _seed_loop(loop)
    _seed_run_without_quality_matrix(run_dir)
    (run_dir / "quality-matrix.json").write_text(json.dumps({
        "schema": "simplicio.quality-matrix/v1", "coverage_threshold": 85,
        "requirements": {}, "coverage": {"measured": 99.0},
    }), encoding="utf-8")
    result = evaluate_completion(str(loop), str(run_dir), response_text="<promise>DONE</promise>")
    assert result["ready"] is False
    assert result["reason_code"].startswith("quality_")
    assert result["reason_code"].endswith("_missing")


def test_partial_evidence_one_lane_short_is_still_rejected(tmp_path):
    """Every required lane individually gates closure — five of six passing is not enough."""
    for missing in REQUIRED_REQUIREMENTS:
        receipt = {
            "schema": "simplicio.quality-matrix/v1",
            "coverage_threshold": 85,
            "requirements": {
                name: {"status": "pass", "proof_ref": f"tests/{name}"}
                for name in REQUIRED_REQUIREMENTS if name != missing
            },
            "coverage": {"measured": 90.0},
        }
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "quality-matrix.json").write_text(json.dumps(receipt), encoding="utf-8")
            result = evaluate_quality_matrix(str(run_dir))
            assert result["ready"] is False, missing
            assert result["reason_code"] == f"quality_{missing}_missing", missing


def test_coverage_just_under_default_threshold_is_rejected(tmp_path):
    """84.999...% must not round up to the 85% default minimum."""
    loop = tmp_path / "loop"; loop.mkdir()
    run_dir = tmp_path / "run"; run_dir.mkdir()
    _seed_loop(loop)
    _seed_run_without_quality_matrix(run_dir)
    (run_dir / "quality-matrix.json").write_text(json.dumps({
        "schema": "simplicio.quality-matrix/v1",
        "requirements": {
            name: {"status": "pass", "proof_ref": f"tests/{name}"} for name in REQUIRED_REQUIREMENTS
        },
        "coverage": {"measured": 84.99},
    }), encoding="utf-8")
    result = evaluate_completion(str(loop), str(run_dir), response_text="<promise>DONE</promise>")
    assert result["ready"] is False
    assert result["reason_code"] == "coverage_below_threshold"


if __name__ == "__main__":
    sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_quality_matrix_regression")
