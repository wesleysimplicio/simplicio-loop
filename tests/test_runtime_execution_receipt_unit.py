import copy
import time

import pytest

from simplicio_loop.receipt_verifier import ReceiptStatus, verify_receipt
from simplicio_loop.runtime_execution_receipt import (
    RUNTIME_EXECUTION_RECEIPT_SCHEMA,
    RuntimeExecutionReceiptError,
    UNAVAILABLE,
    build_runtime_execution_receipt,
)


def _base_kwargs(**overrides):
    base = dict(
        route_id="route-1",
        requested={"runtime": "codex", "provider": "openai", "model_id": "gpt-5.4"},
        resolved={"runtime": "codex", "provider": "openai", "model_id": "gpt-5.4"},
        driver={"name": "codex-cli", "binary": "codex", "version": "1.2.3", "identity_verified": True},
        session={"worker_id": "w1", "device_id": "d1", "attempt_id": "a1", "lease_id": "l1", "fence_token": "f1"},
        argv_redacted=["codex", "exec", "--non-interactive"],
        env_allowlist=["PATH", "HOME"],
        tree={"base_sha": "abc123", "head_sha": "def456", "changed_paths": ["a.py", "b.py"]},
        exit_status=0,
        duration_seconds=12.5,
        stop_reason="completed",
    )
    base.update(overrides)
    return base


def test_build_receipt_has_expected_schema_and_verifies():
    receipt = build_runtime_execution_receipt(**_base_kwargs())
    assert receipt["schema"] == "simplicio.runtime-execution-receipt/v1"
    assert receipt["route_id"] == "route-1"
    assert receipt["requested"]["model_id"] == "gpt-5.4"
    assert receipt["resolved"]["model_id"] == "gpt-5.4"
    assert receipt["receipt_sha"]

    verdict = verify_receipt(receipt, schema=RUNTIME_EXECUTION_RECEIPT_SCHEMA)
    assert verdict.status == ReceiptStatus.VERIFIED
    assert verdict.verified is True


def test_unresolved_model_is_unavailable_not_fabricated():
    receipt = build_runtime_execution_receipt(**_base_kwargs(resolved=None))
    assert receipt["resolved"] == {
        "runtime": UNAVAILABLE, "provider": UNAVAILABLE, "model_id": UNAVAILABLE, "verified": False,
    }
    verdict = verify_receipt(receipt, schema=RUNTIME_EXECUTION_RECEIPT_SCHEMA)
    assert verdict.verified is True  # resolved=UNAVAILABLE is a valid, honest state


def test_missing_usage_fields_default_to_unavailable_not_zero():
    receipt = build_runtime_execution_receipt(**_base_kwargs(usage=None))
    assert receipt["usage"] == {
        "tokens": UNAVAILABLE, "cost_usd": UNAVAILABLE, "latency_seconds": UNAVAILABLE,
    }
    receipt2 = build_runtime_execution_receipt(**_base_kwargs(usage={"tokens": 1234}))
    assert receipt2["usage"]["tokens"] == 1234
    assert receipt2["usage"]["cost_usd"] == UNAVAILABLE


def test_tamper_detection_via_recomputed_hash():
    receipt = build_runtime_execution_receipt(**_base_kwargs())
    tampered = copy.deepcopy(receipt)
    tampered["exit_status"] = 1  # attacker/bug flips a field without recomputing receipt_sha
    verdict = verify_receipt(tampered, schema=RUNTIME_EXECUTION_RECEIPT_SCHEMA)
    assert verdict.status == ReceiptStatus.TAMPERED


def test_missing_provenance_field_is_rejected():
    receipt = build_runtime_execution_receipt(**_base_kwargs())
    receipt["session"]["attempt_id"] = ""
    # Recompute hash after mutating so this exercises provenance rejection, not tamper detection.
    from simplicio_loop.runtime_execution_receipt import _stable_hash
    content_fields = [k for k in receipt if k != "receipt_sha"]
    receipt["receipt_sha"] = _stable_hash({k: receipt[k] for k in content_fields})
    verdict = verify_receipt(receipt, schema=RUNTIME_EXECUTION_RECEIPT_SCHEMA)
    assert verdict.status == ReceiptStatus.MISSING_FIELD
    assert "attempt_id" in verdict.reason


def test_stale_receipt_is_rejected_with_max_age():
    receipt = build_runtime_execution_receipt(**_base_kwargs())
    verdict = verify_receipt(
        receipt, schema=RUNTIME_EXECUTION_RECEIPT_SCHEMA,
        max_age_seconds=1.0, now=time.time() + 1_000_000.0,
    )
    assert verdict.status == ReceiptStatus.STALE


def test_build_rejects_bad_route_id_and_stop_reason_and_negative_duration():
    with pytest.raises(RuntimeExecutionReceiptError, match="route_id"):
        build_runtime_execution_receipt(**_base_kwargs(route_id=""))
    with pytest.raises(RuntimeExecutionReceiptError, match="stop_reason"):
        build_runtime_execution_receipt(**_base_kwargs(stop_reason="made_up_reason"))
    with pytest.raises(RuntimeExecutionReceiptError, match="duration_seconds"):
        build_runtime_execution_receipt(**_base_kwargs(duration_seconds=-1.0))


def test_fallback_linkage_fields_are_recorded():
    receipt = build_runtime_execution_receipt(
        **_base_kwargs(previous_route_id="route-0", fallback_reason_code="timeout")
    )
    assert receipt["previous_route_id"] == "route-0"
    assert receipt["fallback_reason_code"] == "timeout"


def test_no_credentials_or_shell_strings_leak_into_argv():
    receipt = build_runtime_execution_receipt(**_base_kwargs(argv_redacted=["codex", "exec", "--flag", "value"]))
    assert receipt["argv_redacted"] == ["codex", "exec", "--flag", "value"]
    # argv is always a structured list, never a single shell string.
    assert all(" && " not in a and "|" not in a for a in receipt["argv_redacted"])
