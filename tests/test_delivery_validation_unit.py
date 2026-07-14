"""Unit coverage for simplicio_loop.delivery.validate_delivery_receipt across every delivery
state and failure branch. Complements tests/test_delivery_source_identity.py (fingerprint
binding) and tests/test_delivery_reconciliation.py (observation reconciliation) which don't
exercise the per-state required-field / regression checks directly.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simplicio_loop.delivery import DELIVERY_SCHEMA, source_fingerprint, validate_delivery_receipt


def _receipt(**overrides):
    base = {"schema": DELIVERY_SCHEMA, "current_state": "implemented", "source_payload": {}}
    base.update(overrides)
    return base


def test_rejects_wrong_schema():
    result = validate_delivery_receipt({"schema": "wrong"})
    assert result["ok"] is False
    assert result["gates"][-1]["reason_code"] == "delivery_schema_invalid"


def test_rejects_unsupported_state():
    result = validate_delivery_receipt(_receipt(current_state="bogus"))
    assert result["ok"] is False
    assert result["gates"][-1]["reason_code"] == "delivery_state_invalid"


def test_implemented_state_has_no_required_fields():
    result = validate_delivery_receipt(_receipt())
    assert result["ok"] is True


def test_verified_state_requires_evidence_and_criteria():
    incomplete = validate_delivery_receipt(_receipt(current_state="verified"))
    assert incomplete["ok"] is False
    assert incomplete["gates"][-1]["reason_code"] == "delivery_source_incomplete"

    complete = validate_delivery_receipt(_receipt(
        current_state="verified",
        source_payload={"evidence_receipt": "evidence-receipt.json", "criteria_verified": 1},
    ))
    assert complete["ok"] is True


def test_pr_open_requires_pr_fields():
    result = validate_delivery_receipt(_receipt(
        current_state="pr-open", source_payload={"pr": {"url": "https://x/pr/1"}},
    ))
    assert result["ok"] is False
    assert result["gates"][-1]["reason_code"] == "delivery_source_incomplete"
    assert "pr.head_sha" in result["gates"][-1]["detail"]


def test_merge_ready_source_checks_not_green_blocks():
    payload = {
        "pr": {"url": "u", "head_sha": "h", "base_sha": "b"},
        "checks": {"green": False},
        "reviews": {"approvals": 1, "open_threads": 0},
        "branch": {"up_to_date": True},
    }
    result = validate_delivery_receipt(_receipt(current_state="merge-ready", source_payload=payload))
    assert result["ok"] is False
    assert result["gates"][-1]["reason_code"] == "checks_not_green"


def test_merge_ready_source_open_review_threads_blocks():
    payload = {
        "pr": {"url": "u", "head_sha": "h", "base_sha": "b"},
        "checks": {"green": True},
        "reviews": {"approvals": 1, "open_threads": 2},
        "branch": {"up_to_date": True},
    }
    result = validate_delivery_receipt(_receipt(current_state="merge-ready", source_payload=payload))
    assert result["ok"] is False
    assert result["gates"][-1]["reason_code"] == "review_threads_open"


def test_merge_ready_source_missing_approvals_blocks():
    payload = {
        "pr": {"url": "u", "head_sha": "h", "base_sha": "b"},
        "checks": {"green": True},
        "reviews": {"approvals": 0, "open_threads": 0},
        "branch": {"up_to_date": True},
    }
    result = validate_delivery_receipt(_receipt(current_state="merge-ready", source_payload=payload))
    assert result["ok"] is False
    assert result["gates"][-1]["reason_code"] == "approvals_missing"


def test_merge_ready_source_branch_drift_blocks():
    payload = {
        "pr": {"url": "u", "head_sha": "h", "base_sha": "b"},
        "checks": {"green": True},
        "reviews": {"approvals": 1, "open_threads": 0},
        "branch": {"up_to_date": False},
    }
    result = validate_delivery_receipt(_receipt(current_state="merge-ready", source_payload=payload))
    assert result["ok"] is False
    assert result["gates"][-1]["reason_code"] == "branch_drift_open"


def test_merge_ready_source_all_clear_passes():
    payload = {
        "pr": {"url": "u", "head_sha": "h", "base_sha": "b"},
        "checks": {"green": True},
        "reviews": {"approvals": 1, "open_threads": 0},
        "branch": {"up_to_date": True},
    }
    result = validate_delivery_receipt(_receipt(current_state="merge-ready", source_payload=payload))
    assert result["ok"] is True


def test_merged_requires_default_branch_visibility():
    payload = {
        "pr": {"url": "u"},
        "merge": {"commit_sha": "c", "default_branch": "main", "merged_at": "t",
                  "commit_in_default_branch": False},
    }
    result = validate_delivery_receipt(_receipt(current_state="merged", source_payload=payload))
    assert result["ok"] is False
    assert result["gates"][-1]["reason_code"] == "merge_not_visible_on_default_branch"

    payload["merge"]["commit_in_default_branch"] = True
    result = validate_delivery_receipt(_receipt(current_state="merged", source_payload=payload))
    assert result["ok"] is True


def test_released_requires_checksums_signatures_sbom_and_smoke_in_order():
    full = {
        "release": {"tag": "v1", "assets": ["a"], "checksums_verified": True,
                    "signatures_verified": True, "sbom_present": True},
        "install_smoke": {"passed": True},
    }

    missing_checksum = dict(full, release=dict(full["release"], checksums_verified=False))
    result = validate_delivery_receipt(_receipt(current_state="released", source_payload=missing_checksum))
    assert result["ok"] is False
    assert result["gates"][-1]["reason_code"] == "release_checksum_missing"

    missing_sig = dict(full, release=dict(full["release"], signatures_verified=False))
    result = validate_delivery_receipt(_receipt(current_state="released", source_payload=missing_sig))
    assert result["ok"] is False
    assert result["gates"][-1]["reason_code"] == "release_signature_missing"

    missing_sbom = dict(full, release=dict(full["release"], sbom_present=False))
    result = validate_delivery_receipt(_receipt(current_state="released", source_payload=missing_sbom))
    assert result["ok"] is False
    assert result["gates"][-1]["reason_code"] == "release_sbom_missing"

    missing_smoke = dict(full, install_smoke={"passed": False})
    result = validate_delivery_receipt(_receipt(current_state="released", source_payload=missing_smoke))
    assert result["ok"] is False
    assert result["gates"][-1]["reason_code"] == "install_smoke_failed"

    result = validate_delivery_receipt(_receipt(current_state="released", source_payload=full))
    assert result["ok"] is True


def test_deployed_requires_smoke_pass():
    payload = {
        "deployment": {"environment": "prod", "verified_at": "t", "smoke": {"passed": False}},
    }
    result = validate_delivery_receipt(_receipt(current_state="deployed", source_payload=payload))
    assert result["ok"] is False
    assert result["gates"][-1]["reason_code"] == "deployment_smoke_failed"

    payload["deployment"]["smoke"]["passed"] = True
    result = validate_delivery_receipt(_receipt(current_state="deployed", source_payload=payload))
    assert result["ok"] is True


def test_target_mismatch_blocks_before_state_checks():
    result = validate_delivery_receipt(
        _receipt(current_state="verified", target="merge-ready"), target="verified",
    )
    assert result["ok"] is False
    assert result["gates"][-1]["reason_code"] == "delivery_target_mismatch"


def test_target_not_met_regresses_pr_open_to_merge_ready_with_specific_reason():
    payload = {
        "pr": {"url": "u", "head_sha": "h", "base_sha": "b"},
        "checks": {"green": False},
        "reviews": {"approvals": 1, "open_threads": 0},
        "branch": {"up_to_date": True},
    }
    result = validate_delivery_receipt(
        _receipt(current_state="pr-open", target="merge-ready", source_payload=payload),
        target="merge-ready",
    )
    assert result["ok"] is False
    assert result["gates"][-1]["reason_code"] == "checks_not_green"


def test_target_not_met_generic_reason_when_state_below_target_and_not_pr_open():
    result = validate_delivery_receipt(
        _receipt(current_state="implemented", target="verified"), target="verified",
    )
    assert result["ok"] is False
    assert result["gates"][-1]["reason_code"] == "delivery_target_not_met"


def test_source_fingerprint_mismatch_blocks():
    payload = {"foo": "bar"}
    receipt = _receipt(source_fingerprint="stale-fingerprint", source_payload=payload)
    result = validate_delivery_receipt(receipt)
    assert result["ok"] is False
    assert result["gates"][-1]["reason_code"] == "source_fingerprint_mismatch"


def test_source_fingerprint_match_passes():
    payload = {"foo": "bar"}
    receipt = _receipt(source_fingerprint=source_fingerprint(payload), source_payload=payload)
    result = validate_delivery_receipt(receipt)
    assert result["ok"] is True
    assert any(g["reason_code"] == "source_fingerprint_valid" for g in result["gates"])


def test_legacy_receipt_without_source_fingerprint_is_readable():
    receipt = _receipt(current_state="verified",
                       source_payload={"evidence_receipt": "e", "criteria_verified": 1})
    result = validate_delivery_receipt(receipt)
    assert result["ok"] is True
    assert any(g["reason_code"] == "source_identity_legacy_unbound" for g in result["gates"])
