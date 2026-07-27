from contextlib import ExitStack
from unittest.mock import patch

import simplicio_loop.completion_auditor as ca
from simplicio_loop.work_gap_ledger import SCHEMA, validate_work_gap_snapshot


def _valid_snapshot():
    return {
        "schema": SCHEMA,
        "gaps": [
            {
                "requirement_id": "REQ-785",
                "acceptance_criterion_id": "AC-1",
                "state": "DELIVERED",
                "owner_project": "simplicio-loop",
                "owner_agent": "agent-1",
                "dependencies": [],
                "expected_evidence": ["test"],
                "delivery_target": "main",
                "executor_id": "executor-1",
                "verifier_id": "verifier-1",
                "completion_auditor_id": "auditor-1",
                "evidence": [],
                "revision": 1,
            }
        ],
        "events": [],
    }


def test_invalid_or_unresolved_snapshot_is_not_ready():
    assert validate_work_gap_snapshot({})["ok"] is False
    unresolved = _valid_snapshot()
    unresolved["gaps"][0]["state"] = "VERIFIED"
    assert validate_work_gap_snapshot(unresolved)["ok"] is False


def test_audit_blocks_terminal_verdict_when_ledger_is_invalid():
    with ExitStack() as stack:
        stack.enter_context(patch.object(ca.sa, "validate_graph", return_value=(True, [])))
        stack.enter_context(patch.object(ca, "validate_stage_lineage", return_value={"stage": {"verdict": "ok", "evidence_refs": []}}))
        stack.enter_context(patch.object(ca, "validate_auditor_isolation", return_value=(True, [])))
        stack.enter_context(patch.object(ca, "build_ac_coverage_matrix", return_value={"contradictory": [], "missing": [], "unverified": [], "rows": []}))
        stack.enter_context(patch.object(ca, "revalidate_watcher", return_value={"ok": True, "reason_code": "ok", "detail": {}}))
        stack.enter_context(patch.object(ca, "revalidate_delivery", return_value={"ok": True, "reason_code": "ok", "detail": {}}))
        stack.enter_context(patch.object(ca, "detect_regression", return_value={"regressed": False}))
        result = ca.audit(
            graph={}, instances=[], receipts=[], run_identity={"run_id": "run-1"},
            auditor_instance_id="auditor-1", ac_items=[], criteria_results=[],
            work_gap_snapshot={},
        )
    assert result["verdict"] == ca.VERDICT_BLOCKED
    assert result["reason_code"] == "work_gap_ledger_invalid"


def test_completion_receipt_hash_includes_work_gap_check():
    base = {
        "verdict": ca.VERDICT_COMPLETE,
        "reason_code": ca.REASON_OK,
        "run_identity": {"run_id": "run-1", "task_id": "task-1"},
        "work_gap_check": validate_work_gap_snapshot(_valid_snapshot()),
    }
    changed = {
        **base,
        "work_gap_check": {"ok": False, "reason_code": "tampered", "detail": {}},
    }
    first = ca.build_completion_receipt(base, created_at="2026-07-27T00:00:00Z")
    second = ca.build_completion_receipt(changed, created_at="2026-07-27T00:00:00Z")
    assert first["evidence_set_hash"] != second["evidence_set_hash"]
    assert first["receipt_id"] != second["receipt_id"]
