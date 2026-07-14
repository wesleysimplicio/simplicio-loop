"""Tests for simplicio_loop/receipt_verifier.py (issue #288).

Covers: a valid receipt -> VERIFIED; a tampered (hash-mismatched) receipt -> TAMPERED; a
stale receipt beyond max age -> STALE; a malformed/missing-field receipt ->
INVALID_SCHEMA/MISSING_FIELD; and the real wired call site in
`simplicio_loop/runner.py::_verify_worker_receipt_pair` (used by
`_operator_dispatch_attempt`) to prove a tampered/stale receipt no longer produces
VERIFIED where before only file existence was checked.
"""
import json
import time

from simplicio_loop import runner
from simplicio_loop.receipt_verifier import (
    EVIDENCE_RECEIPT_SCHEMA,
    OPERATOR_RECEIPT_SCHEMA,
    ReceiptSchema,
    ReceiptStatus,
    canonical_content_hash,
    verify_receipt,
)


def _now_iso(offset_seconds: float = 0.0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset_seconds))


def _operator_receipt(**overrides):
    receipt = {
        "schema": "simplicio.operator-receipt/v0",
        "execution_state": "applied",
        "target": "file.py",
        "measured_at": _now_iso(),
        "source": "live_cli",
        "tool": "simplicio-dev-cli",
        "repo_state_before": {"commit_sha": "deadbeef"},
    }
    receipt.update(overrides)
    return receipt


def _evidence_receipt(**overrides):
    receipt = {
        "schema": "simplicio.evidence-receipt/v1",
        "run_id": "r1",
        "status": "VERIFIED",
        "measured_at": _now_iso(),
        "run": {"commit_sha": "deadbeef"},
        "operator": {"execution_state": "applied", "receipt_path": "/tmp/operator-receipt.json"},
    }
    receipt.update(overrides)
    return receipt


# --- verify_receipt() unit behavior ------------------------------------------------


def test_valid_receipt_is_verified():
    verdict = verify_receipt(_operator_receipt(), schema=OPERATOR_RECEIPT_SCHEMA, max_age_seconds=3600)
    assert verdict.status == ReceiptStatus.VERIFIED
    assert verdict.verified is True
    assert "passed" in verdict.reason


def test_tampered_content_hash_mismatch_is_detected():
    receipt = _operator_receipt()
    genuine_hash = canonical_content_hash(receipt, OPERATOR_RECEIPT_SCHEMA.content_fields)
    receipt["receipt_sha"] = genuine_hash
    # Tamper with a field *after* the hash was declared -- the classic forged-receipt case.
    receipt["execution_state"] = "applied_but_secretly_failed"
    verdict = verify_receipt(receipt, schema=OPERATOR_RECEIPT_SCHEMA, max_age_seconds=3600)
    assert verdict.status == ReceiptStatus.TAMPERED
    assert "hash mismatch" in verdict.reason


def test_tampered_via_explicit_expected_hash_mismatch():
    receipt = _operator_receipt()
    verdict = verify_receipt(
        receipt, schema=OPERATOR_RECEIPT_SCHEMA, expected_hash="0" * 64, max_age_seconds=3600,
    )
    assert verdict.status == ReceiptStatus.TAMPERED


def test_stale_receipt_beyond_max_age_is_rejected():
    old_receipt = _operator_receipt(measured_at=_now_iso(offset_seconds=-7200))
    verdict = verify_receipt(old_receipt, schema=OPERATOR_RECEIPT_SCHEMA, max_age_seconds=3600)
    assert verdict.status == ReceiptStatus.STALE
    assert "exceeds max_age_seconds" in verdict.reason


def test_future_timestamp_is_treated_as_tampered_not_fresh():
    future_receipt = _operator_receipt(measured_at=_now_iso(offset_seconds=3600))
    verdict = verify_receipt(future_receipt, schema=OPERATOR_RECEIPT_SCHEMA, max_age_seconds=3600)
    assert verdict.status == ReceiptStatus.TAMPERED
    assert "future" in verdict.reason


def test_missing_required_field_is_missing_field():
    receipt = _operator_receipt()
    del receipt["measured_at"]
    verdict = verify_receipt(receipt, schema=OPERATOR_RECEIPT_SCHEMA, max_age_seconds=3600)
    assert verdict.status == ReceiptStatus.MISSING_FIELD
    assert "measured_at" in verdict.reason


def test_empty_provenance_field_is_missing_field():
    receipt = _operator_receipt(tool="")
    verdict = verify_receipt(receipt, schema=OPERATOR_RECEIPT_SCHEMA, max_age_seconds=3600)
    assert verdict.status == ReceiptStatus.MISSING_FIELD
    assert "tool" in verdict.reason


def test_wrong_schema_id_is_invalid_schema():
    receipt = _operator_receipt(schema="not-the-right-schema/v9")
    verdict = verify_receipt(receipt, schema=OPERATOR_RECEIPT_SCHEMA, max_age_seconds=3600)
    assert verdict.status == ReceiptStatus.INVALID_SCHEMA


def test_empty_receipt_is_missing_field():
    verdict = verify_receipt({}, schema=OPERATOR_RECEIPT_SCHEMA)
    assert verdict.status == ReceiptStatus.MISSING_FIELD


def test_unparseable_timestamp_is_invalid_schema():
    receipt = _operator_receipt(measured_at="not-a-timestamp")
    verdict = verify_receipt(receipt, schema=OPERATOR_RECEIPT_SCHEMA, max_age_seconds=3600)
    assert verdict.status == ReceiptStatus.INVALID_SCHEMA


def test_evidence_receipt_schema_valid_case():
    verdict = verify_receipt(_evidence_receipt(), schema=EVIDENCE_RECEIPT_SCHEMA, max_age_seconds=3600)
    assert verdict.status == ReceiptStatus.VERIFIED


def test_canonical_content_hash_is_stable_regardless_of_key_order():
    receipt_a = {"b": 2, "a": 1}
    receipt_b = {"a": 1, "b": 2}
    assert canonical_content_hash(receipt_a) == canonical_content_hash(receipt_b)


def test_no_declared_hash_skips_tamper_check_but_still_verifies():
    # A receipt with no self-declared hash and no expected_hash argument cannot be checked
    # for tampering via hash comparison; it should still verify on schema/freshness/provenance.
    verdict = verify_receipt(_operator_receipt(), schema=OPERATOR_RECEIPT_SCHEMA, max_age_seconds=3600)
    assert verdict.status == ReceiptStatus.VERIFIED


# --- real wired call site: simplicio_loop/runner.py::_verify_worker_receipt_pair --------


def test_wired_call_site_verifies_a_genuine_receipt_pair(tmp_path):
    operator_path = tmp_path / "operator-receipt.json"
    evidence_path = tmp_path / "evidence-receipt.json"
    operator_path.write_text(json.dumps(_operator_receipt()), encoding="utf-8")
    evidence_path.write_text(
        json.dumps(_evidence_receipt(operator={"execution_state": "applied", "receipt_path": str(operator_path)})),
        encoding="utf-8",
    )
    result = runner._verify_worker_receipt_pair(str(operator_path), str(evidence_path))
    assert result["status"] == ReceiptStatus.VERIFIED


def test_wired_call_site_rejects_existence_only_empty_receipts(tmp_path):
    """Before this change, two existing-but-empty `{}` files reported VERIFIED. Now they must not."""
    operator_path = tmp_path / "operator-receipt.json"
    evidence_path = tmp_path / "evidence-receipt.json"
    operator_path.write_text("{}", encoding="utf-8")
    evidence_path.write_text("{}", encoding="utf-8")
    result = runner._verify_worker_receipt_pair(str(operator_path), str(evidence_path))
    assert result["status"] != ReceiptStatus.VERIFIED
    assert result["status"] == ReceiptStatus.MISSING_FIELD


def test_wired_call_site_rejects_stale_operator_receipt(tmp_path):
    operator_path = tmp_path / "operator-receipt.json"
    evidence_path = tmp_path / "evidence-receipt.json"
    operator_path.write_text(
        json.dumps(_operator_receipt(measured_at=_now_iso(offset_seconds=-90000))), encoding="utf-8",
    )
    evidence_path.write_text(json.dumps(_evidence_receipt()), encoding="utf-8")
    result = runner._verify_worker_receipt_pair(str(operator_path), str(evidence_path))
    assert result["status"] == ReceiptStatus.STALE
    assert "operator receipt" in result["reason"]


def test_wired_call_site_rejects_tampered_evidence_receipt(tmp_path):
    operator_path = tmp_path / "operator-receipt.json"
    evidence_path = tmp_path / "evidence-receipt.json"
    operator_path.write_text(json.dumps(_operator_receipt()), encoding="utf-8")
    evidence = _evidence_receipt()
    genuine_hash = canonical_content_hash(evidence, EVIDENCE_RECEIPT_SCHEMA.content_fields)
    evidence["receipt_sha"] = genuine_hash
    evidence["status"] = "VERIFIED_BUT_TAMPERED"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    result = runner._verify_worker_receipt_pair(str(operator_path), str(evidence_path))
    assert result["status"] == ReceiptStatus.TAMPERED
    assert "evidence receipt" in result["reason"]


def test_wired_call_site_reports_unverified_when_a_path_is_missing(tmp_path):
    operator_path = tmp_path / "operator-receipt.json"
    operator_path.write_text(json.dumps(_operator_receipt()), encoding="utf-8")
    result = runner._verify_worker_receipt_pair(str(operator_path), str(tmp_path / "missing.json"))
    assert result["status"] == "UNVERIFIED"
