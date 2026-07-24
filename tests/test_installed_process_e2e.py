import pytest

from simplicio_loop.installed_runtime_e2e import run_installed_process_smoke


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
    assert report["negative_lanes"]["direct_mutation_bypass"] == "BLOCKED_NOT_ATTEMPTED"


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
