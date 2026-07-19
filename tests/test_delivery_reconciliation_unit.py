from simplicio_loop.delivery import (
    RECONCILIATION_SCHEMA,
    source_fingerprint,
    validate_delivery_receipt,
    reconcile_delivery_observation,
)


def _receipt(state, payload, *, ready=True):
    receipt = {
        "schema": "simplicio.delivery-receipt/v1",
        "target": "merge-ready",
        "current_state": state,
        "source_payload": payload,
        "source_fingerprint": source_fingerprint(payload),
        "ready": ready,
    }
    receipt["gates"] = validate_delivery_receipt(receipt, target="merge-ready")["gates"]
    return receipt


def _merge_ready_payload():
    return {
        "pr": {"url": "https://example.test/pr/1", "head_sha": "h1", "base_sha": "b1", "evidence": "proof"},
        "checks": {"green": True},
        "reviews": {"approvals": 1, "open_threads": 0},
        "branch": {"up_to_date": True},
    }


def test_ci_race_reopens_a_previously_ready_delivery():
    ready = _receipt("merge-ready", _merge_ready_payload())
    regressed_payload = dict(_merge_ready_payload(), checks={"green": False})
    regressed = _receipt("pr-open", regressed_payload, ready=False)

    result = reconcile_delivery_observation(ready, regressed)

    assert result["schema"] == RECONCILIATION_SCHEMA
    assert result["status"] == "reopened"
    assert result["reason_code"] == "checks_not_green"
    assert result["previous_fingerprint"] != result["current_fingerprint"]


def test_optimistic_reconcile_rejects_a_concurrent_previous_receipt():
    ready = _receipt("merge-ready", _merge_ready_payload())
    current = _receipt("merge-ready", _merge_ready_payload())

    result = reconcile_delivery_observation(
        ready, current, expected_previous_fingerprint="writer-read-another-version"
    )

    assert result["status"] == "stale"
    assert result["reason_code"] == "previous_observation_changed"


def test_identical_observation_is_idempotent():
    ready = _receipt("merge-ready", _merge_ready_payload())
    result = reconcile_delivery_observation(ready, ready)
    assert result["status"] == "unchanged"
    assert result["reason_code"] == "same_source_observation"


def test_first_observation_is_not_mistaken_for_external_delivery_proof():
    current = _receipt("pr-open", _merge_ready_payload(), ready=False)
    result = reconcile_delivery_observation(None, current)
    assert result["status"] == "observed"
    assert result["reason_code"] == "fresh_observation"
