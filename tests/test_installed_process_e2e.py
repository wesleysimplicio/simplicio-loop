import pytest

from simplicio_loop import installed_process_e2e
from simplicio_loop.installed_process_e2e import (
    NEGATIVE_LANES,
    run_installed_process_smoke,
    verify_negative_lane,
    verify_watcher_receipt,
)
from simplicio_loop.installed_e2e_gates import InstalledGateError


@pytest.mark.external_integration
def test_real_installed_component_process_chain_is_causal_and_fail_closed(tmp_path):
    report = run_installed_process_smoke(str(tmp_path), timeout_seconds=20)
    assert report["installed"] is True
    assert (
        report["status"] == "BLOCKED"
    )  # watcher/HBP are intentionally absent in this host setup
    assert report["effects_attempted"] is False
    assert report["effects_authorized"] is False
    assert report["components"]["mapper"]["status"] in {
        "READY",
        "UNAVAILABLE",
        "BLOCKED",
    }
    assert report["components"]["dev_cli"]["status"] in {
        "READY",
        "UNAVAILABLE",
        "BLOCKED",
    }
    assert report["components"]["runtime"]["status"] in {
        "READY",
        "UNAVAILABLE",
        "BLOCKED",
    }
    assert report["components"]["watcher"]["status"] in {"UNAVAILABLE", "BLOCKED"}
    assert report["components"]["hbp"]["status"] in {"READY", "UNAVAILABLE", "BLOCKED"}
    assert report["metrics"]["process_count"] <= 1
    assert report["metrics"]["latency"]["p50_ms"] is not None
    assert all(
        item["correlation_id"] == report["correlation_id"]
        for item in report["components"].values()
    )
    assert all(item["receipt_hash"] for item in report["components"].values())


def test_missing_installed_binary_blocks_without_authorizing_effects(tmp_path):
    report = run_installed_process_smoke(
        str(tmp_path),
        executable_overrides={"mapper": str(tmp_path / "does-not-exist")},
        timeout_seconds=2,
    )
    assert report["status"] == "BLOCKED"
    assert report["components"]["mapper"]["status"] == "UNAVAILABLE"
    assert report["components"]["mapper"]["reason"] == "binary_missing"
    assert report["effects_attempted"] is False
    assert report["negative_lanes"]["direct_mutation_bypass"] == "REQUIRES_INJECTED_EVIDENCE"


def test_existing_fixture_is_not_mutated_by_the_process_probe(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    marker = fixture / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    before = sorted(path.relative_to(fixture) for path in fixture.rglob("*"))
    run_installed_process_smoke(
        str(tmp_path), fixture_repo=str(fixture), timeout_seconds=2
    )
    assert sorted(path.relative_to(fixture) for path in fixture.rglob("*")) == before
    assert marker.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    ("lane", "reason"),
    [
        ("runtime_missing", "binary_missing"),
        ("wrong_runtime_binary", "product_identity"),
        ("mapper_capability_missing", "mapper_capability_missing"),
        ("dev_cli_capability_missing", "dev_cli_capability_missing"),
        ("disconnect_after_effect", "outcome_unknown"),
        ("corrupt_hbp_link", "hbp_hash_mismatch"),
        ("stale_mapper_artifact", "mapper_artifact_stale"),
        ("watcher_mismatch", "watcher_challenge_mismatch"),
        ("duplicate_idempotency_key", "duplicate_idempotency_key"),
        ("direct_mutation_bypass", "direct_mutation_blocked"),
        ("version_schema_mismatch", "compatibility_mismatch"),
        ("cancellation_restart", "cancelled_not_replayed"),
    ],
)
def test_mandatory_negative_matrix_requires_specific_fail_closed_evidence(lane, reason):
    assert set(NEGATIVE_LANES) == {
        "runtime_missing", "wrong_runtime_binary", "mapper_capability_missing",
        "dev_cli_capability_missing", "disconnect_after_effect", "corrupt_hbp_link",
        "stale_mapper_artifact", "watcher_mismatch", "duplicate_idempotency_key",
        "direct_mutation_bypass", "version_schema_mismatch", "cancellation_restart",
    }
    evidence = {"status": "BLOCKED", "effects_authorized": False, "reason": reason}
    assert verify_negative_lane(lane, evidence)["status"] == "PASS"
    assert verify_negative_lane(lane, {**evidence, "reason": "generic"})["status"] == "FAIL"
    assert verify_negative_lane(lane, {**evidence, "effects_authorized": True})["status"] == "FAIL"


def test_independent_watcher_gate_binds_challenge_run_hbp_and_criteria(monkeypatch, tmp_path):
    monkeypatch.setattr(
        installed_process_e2e.uuid,
        "uuid4",
        lambda: type("RunId", (), {"hex": "causal-run-693"})(),
    )
    receipt = {
        "schema": "simplicio.independent-watcher-receipt/v1",
        "status": "MEASURED",
        "match": True,
        "challenge": "challenge-693",
        "correlation_id": "causal-run-693",
        "hbp_receipt_hash": "hbp-sha256",
        "criteria_results": [{"id": "ac-1", "status": "PASS"}],
        "producer": {"worker": "independent_watcher.py"},
    }
    direct = verify_watcher_receipt(
        receipt, challenge="challenge-693", correlation_id="causal-run-693"
    )
    assert direct["status"] == "READY"

    report = run_installed_process_smoke(
        str(tmp_path),
        watcher_command=["python", "-c", "raise SystemExit(0)"],
        hbp_command=["python", "-c", "raise SystemExit(0)"],
        watcher_receipt=receipt,
        watcher_challenge="challenge-693",
        timeout_seconds=2,
    )
    assert report["components"]["watcher"]["receipt_gate"]["status"] == "READY"

    corrupt = {**receipt, "challenge": "stale"}
    assert verify_watcher_receipt(
        corrupt, challenge="challenge-693", correlation_id="causal-run-693"
    )["reason"].startswith("watcher_receipt_invalid:")
    assert verify_watcher_receipt(
        None, challenge="challenge-693", correlation_id="causal-run-693"
    ) == {"status": "BLOCKED", "reason": "watcher_receipt_missing"}


def test_unknown_negative_lane_is_rejected():
    with pytest.raises(InstalledGateError, match="unknown_negative_lane"):
        verify_negative_lane("not-a-lane", {})
