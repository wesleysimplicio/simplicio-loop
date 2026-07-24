from simplicio_loop.execution_route import decide_route
from simplicio_loop.production_integration import evaluate_production_integration


def evidence():
    route = decide_route("mechanically update the indexed file", True, False).to_dict()
    return {
        "route": route,
        "effect": {"profile": "runtime-backed", "idempotency_key": "k", "lease_id": "l"},
        "context": {"status": "READY"}, "sessions": {"status": "READY"},
        "installed": {"status": "READY"},
    }


def test_full_production_evidence_allows_effects():
    report = evaluate_production_integration(evidence())
    assert report["status"] == "READY"
    assert report["effects_allowed"] is True
    assert len(report["integration_hash"]) == 64


def test_partial_or_standalone_evidence_blocks_without_false_success():
    partial = evidence()
    partial.pop("installed")
    partial["effect"]["profile"] = "standalone"
    report = evaluate_production_integration(partial)
    assert report["status"] == "BLOCKED"
    assert report["effects_allowed"] is False
    assert "missing:installed" in report["reasons"]
    assert "effect_not_runtime_authoritative" in report["reasons"]


def test_route_hash_drift_blocks_integration():
    drifted = evidence()
    drifted["route"]["reason"] = "tampered"
    report = evaluate_production_integration(drifted)
    assert report["status"] == "BLOCKED"
    assert "route_hash_invalid" in report["reasons"]
