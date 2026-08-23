"""Tests for the Loop-owned release-train event reconciler."""

import json
from pathlib import Path

import pytest

from scripts.release_train_reconciler import ReconciliationError, reconcile


def _manifest(
    component: str,
    version: str,
    *,
    package: str | None = None,
    digest: str = "a",
    compatibility: dict | None = None,
) -> dict:
    return {
        "component": component,
        "repo": f"wesleysimplicio/{component}",
        "package": package or component,
        "version": version,
        "commit": f"commit-{version}",
        "tag": f"v{version}",
        "artifacts": [{
            "registry": "pypi",
            "os": "any",
            "arch": "any",
            "digest": "sha256:" + digest * 64,
            "size": 1,
            "signature": "sig:ed25519:test",
        }],
        "compatibility": compatibility or {},
        "breaking_change": False,
        "changelog": [{"version": version, "notes": "test"}],
        "channel": "stable",
    }


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        "dependencies = [\n"
        '  "simplicio-cli>=0.18.10,<0.19",\n'
        '  "simplicio-mapper>=0.26.20,<0.27",\n'
        "]\n",
        encoding="utf-8",
    )
    return tmp_path


def _event(*manifests: dict, release_id: str = "release-1") -> dict:
    return {
        "client_payload": {
            "event_type": "simplicio.component-release.v1",
            "release_id": release_id,
            "graph_hash": "sha256:" + "g" * 64,
            "manifests": list(manifests),
        }
    }


def test_selects_newest_candidate_inside_declared_range_and_writes_lock(tmp_path: Path):
    repo = _repo(tmp_path)
    receipt = reconcile(
        repo,
        _event(
            _manifest("simplicio-cli", "0.18.11", digest="b"),
            _manifest("simplicio-cli", "0.18.12", digest="c"),
            _manifest("simplicio-cli", "0.19.0", digest="d"),
        ),
    )

    assert receipt["status"] == "changed"
    assert receipt["selected"]["simplicio-cli"]["version"] == "0.18.12"
    assert "pyproject.toml" in receipt["changed_files"]
    assert ".simplicio/release-train.lock.json" in receipt["changed_files"]
    content = (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert "simplicio-cli>=0.18.12,<0.19" in content
    lock = json.loads((repo / ".simplicio/release-train.lock.json").read_text())
    assert lock["components"]["simplicio-cli"]["commit"] == "commit-0.18.12"
    assert lock["components"]["simplicio-cli"]["digests"] == ["sha256:" + "c" * 64]


def test_blocks_candidate_outside_declared_range_without_mutating_files(tmp_path: Path):
    repo = _repo(tmp_path)
    before = (repo / "pyproject.toml").read_text(encoding="utf-8")
    receipt = reconcile(repo, _event(_manifest("simplicio-cli", "0.19.0")))

    assert receipt["status"] == "blocked"
    assert receipt["reason"] == "no_candidate_in_declared_range"
    assert (repo / "pyproject.toml").read_text(encoding="utf-8") == before
    assert not (repo / ".simplicio" / "release-train.lock.json").exists()


def test_blocks_invalid_artifact_before_any_file_change(tmp_path: Path):
    repo = _repo(tmp_path)
    manifest = _manifest("simplicio-cli", "0.18.12")
    manifest["artifacts"][0]["signature"] = ""
    before = (repo / "pyproject.toml").read_text(encoding="utf-8")

    with pytest.raises(ReconciliationError, match="invalid"):
        reconcile(repo, _event(manifest))

    assert (repo / "pyproject.toml").read_text(encoding="utf-8") == before
    assert not (repo / ".simplicio" / "release-train.lock.json").exists()


def test_ignores_release_for_non_direct_dependency(tmp_path: Path):
    repo = _repo(tmp_path)
    receipt = reconcile(repo, _event(_manifest("simplicio-runtime", "3.5.3")))

    assert receipt["status"] == "ignored"
    assert receipt["reason"] == "event_not_a_direct_dependency"
    assert not (repo / ".simplicio" / "release-train.lock.json").exists()


def test_updates_package_name_when_component_uses_repository_name(tmp_path: Path):
    repo = _repo(tmp_path)
    receipt = reconcile(
        repo,
        _event(_manifest("simplicio-dev-cli", "0.18.12", package="simplicio-cli")),
    )

    assert receipt["status"] == "changed"
    assert receipt["selected"]["simplicio-cli"]["component"] == "simplicio-dev-cli"
    assert "simplicio-cli>=0.18.12,<0.19" in (
        repo / "pyproject.toml"
    ).read_text(encoding="utf-8")
