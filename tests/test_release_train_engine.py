"""Unit coverage for the manifest-driven #558 composition engine."""

import json
from pathlib import Path

import pytest

from scripts import release_train as rt


GRAPH_HASH = "sha256:" + "g" * 64
CONTRACTS = {
    "simplicio.component-release/v1": "sha256:" + "c" * 64,
    "simplicio.ecosystem-release/v1": "sha256:" + "e" * 64,
}
EVIDENCE = {"conformance": "passed", "installation": "passed", "e2e": "passed"}


def _manifest(
    component: str,
    version: str,
    *,
    channel: str = "canary",
    compatibility=None,
    digest_char: str = "a",
    breaking_change: bool = False,
    migrations=None,
):
    manifest = {
        "component": component,
        "repo": f"wesleysimplicio/{component}",
        "package": component,
        "version": version,
        "commit": f"commit-{version}",
        "tag": f"v{version}",
        "artifacts": [{
            "registry": "pypi",
            "os": "any",
            "arch": "any",
            "digest": "sha256:" + digest_char * 64,
            "size": 42,
            "signature": "sig:ed25519:test",
        }],
        "compatibility": compatibility or {},
        "breaking_change": breaking_change,
        "changelog": [{"version": version, "notes": "release train test"}],
        "channel": channel,
    }
    if migrations is not None:
        manifest["migrations"] = migrations
    return manifest


def _compose(release_id: str, *, channel: str = "canary"):
    manifests = [
        _manifest("simplicio-cli", "0.18.9", channel=channel, digest_char="b"),
        _manifest(
            "simplicio-loop",
            "3.44.0",
            channel=channel,
            compatibility={"simplicio-cli": ">=0.18.9,<0.19"},
            digest_char="c",
        ),
    ]
    return rt.compose_release(
        manifests,
        release_id=release_id,
        graph_hash=GRAPH_HASH,
        contract_hashes=CONTRACTS,
        evidence=EVIDENCE,
        signature="sig:ed25519:composition",
        channel=channel,
        required_components=("simplicio-cli", "simplicio-loop"),
    )


def test_range_evaluation_is_fail_closed_for_unknown_versions():
    assert rt.satisfies_range("0.18.9", ">=0.18.9,<0.19") is True
    assert rt.satisfies_range("0.19.0", ">=0.18.9,<0.19") is False
    assert rt.satisfies_range("not-semver", ">=0.18.9") is None


def test_compose_builds_hashed_artifacts_and_validates_compatibility():
    composition = _compose("rel-1")

    assert composition["schema"] == rt.ECOSYSTEM_SCHEMA
    assert composition["components"]["simplicio-cli"]["digest"].startswith("sha256:")
    assert composition["provenance"]["composition_hash"].startswith("sha256:")
    assert rt.validate_composition(composition, expected_graph_hash=GRAPH_HASH) == []


def test_compose_blocks_incompatible_dependency():
    manifests = [
        _manifest("simplicio-cli", "0.18.9"),
        _manifest("simplicio-loop", "3.44.0", compatibility={"simplicio-cli": ">=0.19.0"}),
    ]
    with pytest.raises(rt.ReleaseTrainError, match="out_of_range"):
        rt.compose_release(
            manifests,
            release_id="blocked",
            graph_hash=GRAPH_HASH,
            contract_hashes=CONTRACTS,
            evidence=EVIDENCE,
            signature="sig",
        )


def test_compose_blocks_unsigned_or_unproven_release():
    with pytest.raises(rt.ReleaseTrainError, match="unsigned"):
        rt.compose_release(
            [_manifest("simplicio-loop", "3.44.0")],
            release_id="unsigned",
            graph_hash=GRAPH_HASH,
            contract_hashes=CONTRACTS,
            evidence=EVIDENCE,
            signature="",
        )
    with pytest.raises(rt.ReleaseTrainError, match="evidence.e2e"):
        rt.compose_release(
            [_manifest("simplicio-loop", "3.44.0")],
            release_id="unproven",
            graph_hash=GRAPH_HASH,
            contract_hashes=CONTRACTS,
            evidence={"conformance": "passed", "installation": "passed"},
            signature="sig",
        )


def test_breaking_change_requires_migration_plan():
    manifest = _manifest("simplicio-loop", "4.0.0", breaking_change=True)
    with pytest.raises(rt.ReleaseTrainError, match="migrations"):
        rt.compose_release(
            [manifest], release_id="breaking", graph_hash=GRAPH_HASH,
            contract_hashes=CONTRACTS, evidence=EVIDENCE, signature="sig",
        )
    manifest["migrations"] = ["expand", "migrate", "contract"]
    composition = rt.compose_release(
        [manifest], release_id="breaking-ok", graph_hash=GRAPH_HASH,
        contract_hashes=CONTRACTS, evidence=EVIDENCE, signature="sig",
    )
    assert composition["status"]["overall"] == "green"


def test_canary_stable_and_atomic_rollback():
    old_canary = _compose("rel-1", channel="canary")
    state = rt.promote_composition(rt.empty_state(), old_canary, channel="canary")
    old_stable = _compose("rel-1", channel="stable")
    state = rt.promote_composition(
        state, old_stable, channel="stable", canary_evidence={"status": "passed"}
    )
    assert state["stable"]["release_id"] == "rel-1"
    assert state["canary"] is None

    new_canary = _compose("rel-2", channel="canary")
    state = rt.promote_composition(state, new_canary, channel="canary")
    new_stable = _compose("rel-2", channel="stable")
    state = rt.promote_composition(
        state, new_stable, channel="stable", canary_evidence={"status": "passed"}
    )
    assert state["stable"]["release_id"] == "rel-2"
    assert state["history"][0]["release_id"] == "rel-1"

    rolled_back = rt.rollback_composition(state, target_release_id="rel-1")
    assert rolled_back["stable"]["release_id"] == "rel-1"
    assert rolled_back["stable"]["rollout"]["rollback"] is True
    assert rolled_back["canary"] is None


def test_failed_stable_promotion_does_not_mutate_state():
    state = rt.promote_composition(rt.empty_state(), _compose("rel-1"), channel="canary")
    before = json.dumps(state, sort_keys=True)
    with pytest.raises(rt.ReleaseTrainError, match="green canary"):
        rt.promote_composition(
            state,
            _compose("rel-1", channel="stable"),
            channel="stable",
            canary_evidence={"status": "failed"},
        )
    assert json.dumps(state, sort_keys=True) == before


def test_directory_composition_and_atomic_state_file(tmp_path: Path):
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    for manifest in (
        _manifest("simplicio-cli", "0.18.9", digest_char="d"),
        _manifest("simplicio-loop", "3.44.0", compatibility={"simplicio-cli": ">=0.18.9"}, digest_char="e"),
    ):
        (manifests / f"{manifest['component']}.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
    composition = rt.compose_directory(
        manifests,
        release_id="rel-dir",
        graph_hash=GRAPH_HASH,
        contract_hashes=CONTRACTS,
        evidence=EVIDENCE,
        signature="sig",
        channel="canary",
    )
    state_path = tmp_path / "state.json"
    rt.write_json_atomic(state_path, rt.promote_composition(rt.empty_state(), composition, channel="canary"))
    loaded = rt.load_state(state_path)
    assert loaded["canary"]["release_id"] == "rel-dir"
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_directory_selects_newest_compatible_candidate(tmp_path: Path):
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    candidates = [
        _manifest("simplicio-cli", "0.18.9", digest_char="f"),
        _manifest("simplicio-cli", "0.19.0", digest_char="7"),
        _manifest(
            "simplicio-loop", "3.44.0", compatibility={"simplicio-cli": ">=0.18.9,<0.19"},
            digest_char="8",
        ),
    ]
    for index, manifest in enumerate(candidates):
        (manifests / f"{index}-{manifest['component']}.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
    composition = rt.compose_directory(
        manifests,
        release_id="rel-latest-compatible",
        graph_hash=GRAPH_HASH,
        contract_hashes=CONTRACTS,
        evidence=EVIDENCE,
        signature="sig",
        channel="canary",
    )
    # 0.19.0 is newer but outside Loop's declared upper bound, so 0.18.9 is selected.
    assert composition["components"]["simplicio-cli"]["version"] == "0.18.9"
