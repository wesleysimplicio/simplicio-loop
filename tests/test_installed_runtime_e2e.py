import pytest

from simplicio_loop.installed_runtime_e2e import COMPONENTS, InstalledE2EError, run_installed_smoke


def probes(status="READY"):
    return {name: (lambda context, name=name: {"status": status, "component": name}) for name in COMPONENTS}


def test_installed_chain_propagates_correlation_and_authority_without_effects():
    report = run_installed_smoke("C:/repo", probes(), mapper_envelope_hash="mapper", plan_hash="plan")
    assert report["status"] == "READY"
    assert report["effects_attempted"] is False
    assert set(report["components"]) == set(COMPONENTS)
    assert {item["correlation_id"] for item in report["components"].values()} == {report["correlation_id"]}


def test_missing_or_unavailable_component_is_honest_and_stops_chain():
    partial = probes()
    partial["watcher"] = lambda context: {"status": "UNAVAILABLE", "reason": "watcher_missing"}
    report = run_installed_smoke("C:/repo", partial, mapper_envelope_hash="mapper", plan_hash="plan")
    assert report["status"] == "BLOCKED"
    assert report["effects_attempted"] is False
    assert report["components"]["watcher"]["reason"] == "watcher_missing"
    assert "hbp" not in report["components"]


def test_authority_hashes_are_required():
    with pytest.raises(InstalledE2EError, match="hash"):
        run_installed_smoke("C:/repo", probes())
