"""#285 remaining gap: `CLOSE_PENDING_RECONCILIATION` must actually gate `simplicio_loop.oracle`'s
COMPLETE verdict, not be an inert status only a GitHub comment/outbox record ever see.

Builds on the same run/loop artifact fixtures as `tests/test_quality_matrix_integration.py`
(everything else about the run passes) and adds only the persisted lifecycle receipt
(`simplicio_loop.github_lifecycle.persist_lifecycle_receipt`) that
`runner.py::_sync_github_lifecycle` / `scripts/github_lifecycle.py --run-dir` write in real use.
"""
import json

from simplicio_loop.github_lifecycle import persist_lifecycle_receipt
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


def _seed_run(run_dir):
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
    (run_dir / "quality-matrix.json").write_text(json.dumps(_passing_quality_matrix()), encoding="utf-8")


def _completion_kwargs():
    return dict(response_text="<promise>DONE</promise>")


def test_completion_succeeds_when_no_lifecycle_receipt_was_ever_persisted(tmp_path):
    loop = tmp_path / "loop"; loop.mkdir()
    run_dir = tmp_path / "run"; run_dir.mkdir()
    _seed_loop(loop)
    _seed_run(run_dir)
    result = evaluate_completion(str(loop), str(run_dir), **_completion_kwargs())
    assert result["ready"] is True
    assert result["verdict"] == "COMPLETE"
    lifecycle_gates = [g for g in result["gates"] if g["name"] == "source_lifecycle"]
    assert lifecycle_gates and lifecycle_gates[0]["reason_code"] == "source_lifecycle_not_configured"


def test_completion_blocked_when_lifecycle_receipt_reports_close_pending_reconciliation(tmp_path):
    loop = tmp_path / "loop"; loop.mkdir()
    run_dir = tmp_path / "run"; run_dir.mkdir()
    _seed_loop(loop)
    _seed_run(run_dir)
    persist_lifecycle_receipt({
        "schema": "simplicio.github-lifecycle-receipt/v1", "operation_id": "op-1",
        "outcome": "CLOSE_PENDING_RECONCILIATION", "verified": False,
    }, run_dir)

    result = evaluate_completion(str(loop), str(run_dir), **_completion_kwargs())
    assert result["ready"] is False
    assert result["reason_code"] == "source_close_pending_reconciliation"
    assert result["verdict"] != "COMPLETE"


def test_completion_succeeds_once_lifecycle_receipt_is_reconciled(tmp_path):
    loop = tmp_path / "loop"; loop.mkdir()
    run_dir = tmp_path / "run"; run_dir.mkdir()
    _seed_loop(loop)
    _seed_run(run_dir)
    # Simulate `reconcile()` clearing the pending state: the persisted receipt now reports a
    # confirmed close, not a pending reconciliation.
    persist_lifecycle_receipt({
        "schema": "simplicio.github-lifecycle-receipt/v1", "operation_id": "op-1",
        "outcome": "closed", "verified": True,
    }, run_dir)

    result = evaluate_completion(str(loop), str(run_dir), **_completion_kwargs())
    assert result["ready"] is True
    assert result["verdict"] == "COMPLETE"
