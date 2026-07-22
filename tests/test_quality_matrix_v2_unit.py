import copy
import json
from datetime import datetime, timezone

import pytest
from simplicio_loop.quality_matrix_v2 import (LANES, QualityMatrixV2Error, canonical_hash,
                                               evaluate_v2, migrate_v1, schema_text, validate_v2)


def valid_receipt():
    identity = {k: v for k, v in {
        "run_id": "run-1", "task_id": "618", "attempt_id": "attempt-1", "head_sha": "a" * 40,
        "tree_hash": "b" * 64, "diff_hash": "c" * 64, "policy_hash": "d" * 64,
        "config_hash": "e" * 64, "produced_at": "2026-07-22T00:00:00Z"}.items()}
    evidence = {"uri": "receipts/result.json", "sha256": "f" * 64, "run_id": "run-1",
                "attempt_id": "attempt-1", "head_sha": "a" * 40,
                "author": "producer", "auditor": "watcher"}
    return {"schema": "simplicio.quality-matrix/v2", "identity": identity,
            "lanes": {name: {"status": "PASS", "reason_code": "verified",
                              "evidence": [copy.deepcopy(evidence)], "metrics": []} for name in LANES}}


def test_schema_is_packaged_and_closed():
    schema = json.loads(schema_text())
    assert schema["properties"]["lanes"]["additionalProperties"] is False
    assert set(schema["properties"]["lanes"]["required"]) == set(LANES)


def test_valid_full_matrix_passes_and_missing_or_unknown_fails():
    receipt = valid_receipt()
    assert validate_v2(receipt) == []
    assert evaluate_v2(receipt)["ready"] is True
    del receipt["lanes"]["invariants"]
    receipt["surprise"] = True
    errors = validate_v2(receipt)
    assert any("unknown fields" in e for e in errors)
    assert any("missing lanes: invariants" in e for e in errors)


def test_zero_null_and_absent_are_distinct():
    zero = valid_receipt()
    zero["lanes"]["coverage"]["metrics"] = [{"name": "branch", "value": 0, "unit": "percent", "sample_count": 1, "source": "coverage.py", "reason_code": None}]
    assert validate_v2(zero) == []
    null = copy.deepcopy(zero); null["lanes"]["coverage"]["metrics"][0].update(value=None, sample_count=0, reason_code="tool_unavailable")
    assert validate_v2(null) == []
    absent = copy.deepcopy(zero); del absent["lanes"]["coverage"]["metrics"][0]["value"]
    assert any("null value requires" in e for e in validate_v2(absent))


def test_skip_stale_tamper_and_non_independent_evidence_block():
    receipt = valid_receipt(); lane = receipt["lanes"]["unit_component"]
    lane["reason_code"] = "skipped"; ref = lane["evidence"][0]
    ref.update(run_id="old", sha256="not-a-hash", auditor="producer")
    errors = validate_v2(receipt)
    assert any("cannot become PASS" in e for e in errors)
    assert any("stale binding" in e for e in errors)
    assert any("expected lowercase SHA-256" in e for e in errors)
    assert any("auditor must be independent" in e for e in errors)


def test_na_requires_live_independent_policy_bound_waiver():
    receipt = valid_receipt(); lane = receipt["lanes"]["performance_load_stress_soak"]
    lane.update(status="NOT_APPLICABLE", reason_code="not_relevant", evidence=[], waiver={
        "scope": "no runtime path", "justification": "documentation-only", "approver": "quality-owner",
        "expires_at": "2027-01-01T00:00:00Z", "policy_hash": "d" * 64})
    assert validate_v2(receipt, now=datetime(2026, 7, 22, tzinfo=timezone.utc)) == []
    lane["waiver"]["expires_at"] = "2020-01-01T00:00:00Z"
    assert any("expired" in e for e in validate_v2(receipt, now=datetime(2026, 7, 22, tzinfo=timezone.utc)))


def test_v1_migration_never_invents_pass_and_is_deterministic():
    old = {"schema": "simplicio.quality-matrix/v1", "run_id": "r", "work_item": {"id": "618", "head_sha": "a" * 40},
           "requirements": {"unit": {"status": "pass", "proof_ref": "x"}, "system": {"status": "fail"}},
           "coverage": {"measured": 0}}
    one, two = migrate_v1(old), migrate_v1(old)
    assert one == two
    assert all(lane["status"] != "PASS" for lane in one["lanes"].values())
    assert one["lanes"]["unit_component"]["status"] == "BLOCKED"
    assert one["lanes"]["system_e2e"]["status"] == "FAIL"
    assert one["lanes"]["coverage"]["metrics"][0]["value"] == 0


def test_malformed_shapes_and_lane_values_fail_closed():
    assert validate_v2(None) == ["$: expected object"]
    receipt = valid_receipt(); receipt["schema"] = "wrong"; receipt["identity"] = []
    receipt["lanes"]["unit_component"] = []
    errors = validate_v2(receipt)
    assert any("expected simplicio" in e for e in errors)
    assert sum("expected object" in e for e in errors) >= 2
    receipt = valid_receipt(); lane = receipt["lanes"]["coverage"]
    lane.update(status="SKIPPED", reason_code="", evidence={}, metrics={})
    errors = validate_v2(receipt)
    assert any("invalid terminal status" in e for e in errors)
    assert any("reason_code: required" in e for e in errors)
    assert any("evidence: expected array" in e for e in errors)
    assert any("metrics: expected array" in e for e in errors)


def test_metric_and_evidence_required_fields_are_closed():
    receipt = valid_receipt(); lane = receipt["lanes"]["coverage"]
    lane["evidence"] = [{"uri": "x", "extra": 1}]
    lane["metrics"] = [{"name": "", "value": "zero", "unit": "", "sample_count": -1,
                        "source": "", "reason_code": None, "extra": True}]
    errors = validate_v2(receipt)
    assert any("unknown fields" in e for e in errors)
    assert any("required non-empty" in e for e in errors)
    assert any("expected number or null" in e for e in errors)
    assert any("non-negative integer" in e for e in errors)


def test_blocked_and_failed_lanes_are_reported():
    receipt = valid_receipt()
    receipt["lanes"]["invariants"].update(status="BLOCKED", reason_code="unavailable", evidence=[])
    receipt["lanes"]["negative_paths"].update(status="FAIL", reason_code="test_failed")
    verdict = evaluate_v2(receipt)
    assert verdict["ready"] is False
    assert verdict["blocked_lanes"] == ["negative_paths", "invariants"]


def test_waiver_rejects_missing_bad_timestamp_policy_self_approval_and_wrong_placement():
    receipt = valid_receipt(); lane = receipt["lanes"]["implementation"]
    lane["waiver"] = {}; assert any("allowed only" in e for e in validate_v2(receipt))
    lane.update(status="NOT_APPLICABLE", evidence=[], waiver={"scope": "x", "justification": "x",
        "approver": "618", "expires_at": "nonsense", "policy_hash": "wrong"})
    errors = validate_v2(receipt)
    assert any("policy mismatch" in e for e in errors)
    assert any("self-approval" in e for e in errors)
    assert any("invalid timestamp" in e for e in errors)


def test_migration_rejects_wrong_version_and_handles_absent_coverage():
    with pytest.raises(QualityMatrixV2Error):
        migrate_v1({"schema": "other"})
    migrated = migrate_v1({"schema": "simplicio.quality-matrix/v1"})
    metric = migrated["lanes"]["coverage"]["metrics"][0]
    assert metric["value"] is None and metric["reason_code"] == "unavailable_in_v1"
    assert canonical_hash({"b": 2, "a": 1}) == canonical_hash({"a": 1, "b": 2})
