from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

from simplicio_loop import stage_agents as sa

ROOT = Path(__file__).parents[1]
CANON = json.loads((ROOT / "contracts/stage-agents/v1/stages.json").read_text())


def _pair(created="2026-07-16T00:00:00Z"):
    instance = {"agent_instance_id": "i", "role_id": "intake_planner", "role_version": "1.0.0", "stage_id": "intake", "stage_version": "1.0.0", "run_id": "run", "task_id": "task", "work_item_id": "work-item", "attempt_id": "att", "attempt_ordinal": 1, "fence": "fence", "plan_revision": 0, "runtime": "python", "provider": "local", "model": "none", "driver": "portable", "parent_agent_id": "coord", "coordinator_agent_id": "coord", "parent_instance_id": "parent", "idempotency_key": "idem", "isolation_level": "process", "negotiated_capabilities": [], "context_hash": "0" * 64, "manifest_hash": "1" * 64, "terminal_status": "completed"}
    receipt = {**instance, "schema": "simplicio.stage-receipt/v1", "receipt_id": "receipt", "created_at": created, "observed_at": created, "ttl_seconds": 3600, "verdict": "pass", "evidence_refs": ["evidence://test"], "accepted": True, "reason_code": "ok", "input_hash": "2" * 64, "output_hash": "3" * 64, "integrity_hash": "0" * 64, "previous_receipt_hashes": [], "covered_acceptance_criteria": ["AC-test"], "commands": ["pytest -q"], "exit_codes": {"pytest -q": 0}, "artifact_refs": ["artifact://test"], "next_stage_recommendation": "none"}
    receipt["integrity_hash"] = sa.receipt_integrity_hash(receipt)
    return receipt, instance


def test_graph_rejects_orphan_and_skip_edges():
    orphan = deepcopy(CANON)
    orphan["stages"].append({"stage_id": "orphan", "depends_on": [], "next_stages": []})
    ok, errors = sa.validate_graph(orphan)
    assert not ok and any("root" in error or "orphan" in error for error in errors)
    skip = deepcopy(CANON)
    skip["stages"][0]["next_stages"] = ["done"]
    ok, errors = sa.validate_graph(skip)
    assert not ok and any("next_stages" in error for error in errors)


def test_graph_rejects_minimal_stage_without_contract_fields():
    minimal = {"schema": "simplicio.run-stage-graph/v1", "graph_id": "x", "version": "1.0.0", "roles": [{"role_id": "role"}], "stages": [{"stage_id": "stage", "depends_on": [], "next_stages": []}]}
    ok, errors = sa.validate_graph(minimal)
    assert not ok and any("missing schema" in error for error in errors)


def test_graph_rejects_malformed_dependency_type_without_traceback():
    malformed = deepcopy(CANON)
    malformed["stages"][0]["depends_on"] = 42
    ok, errors = sa.validate_graph(malformed)
    assert not ok and any("depends_on" in error for error in errors)


def test_receipt_rejects_ancient_and_manifest_drift():
    receipt, instance = _pair("2000-01-01T00:00:00Z")
    ok, errors = sa.validate_receipt(receipt, instance, now=datetime(2026, 7, 16, tzinfo=timezone.utc))
    assert not ok and any("stale" in error for error in errors)


def test_receipt_rejects_unknown_fields_and_graph_owner_mismatch():
    receipt, instance = _pair()
    receipt["unexpected"] = "input"
    ok, errors = sa.validate_receipt(receipt, instance)
    assert not ok and any("unknown fields" in error for error in errors)
    receipt, instance = _pair()
    receipt["manifest_hash"] = CANON["manifest_hash"]
    instance["manifest_hash"] = CANON["manifest_hash"]
    receipt["role_id"] = "review_panel"
    instance["role_id"] = "review_panel"
    ok, errors = sa.validate_receipt(receipt, instance, CANON, now=datetime(2026, 7, 16, 0, 1, tzinfo=timezone.utc))
    assert not ok and any("does not own graph stage" in error for error in errors)
    receipt, instance = _pair()
    receipt["manifest_hash"] = "2" * 64
    ok, errors = sa.validate_receipt(receipt, instance)
    assert not ok and any("provenance" in error for error in errors)
    receipt, instance = _pair()
    receipt["role_id"] = "review_panel"
    ok, errors = sa.validate_receipt(receipt, instance)
    assert not ok and any("role_id" in error for error in errors)
    receipt, instance = _pair()
    receipt["observed_at"] = "2026-07-16T01:00:00Z"
    ok, errors = sa.validate_receipt(receipt, instance, now=datetime(2026, 7, 16, tzinfo=timezone.utc))
    assert not ok and any("stale" in error for error in errors)
