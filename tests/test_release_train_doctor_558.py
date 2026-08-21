import json
from pathlib import Path

from scripts.release_train import CHILD_BLOCKERS, doctor_release_train


def test_doctor_does_not_claim_eight_repo_conformance():
    report = doctor_release_train()
    assert report["schema"] == "simplicio.release-train-doctor/v1"
    assert report["loop_engine"]["status"] == "MEASURED"
    assert report["loop_engine"]["compose"] is True
    assert report["eight_repo_conformance"] == "UNVERIFIED"
    assert report["closes_eight_repo_ac"] is False
    names = {item["component"] for item in report["children"]}
    assert "simplicio-agent" in names
    assert "simplicio-loop-oss" in names
    assert all(item["status"] == "UNVERIFIED" for item in report["children"])


def _manifest(component: str) -> dict:
    return {
        "component": component,
        "repo": f"https://github.com/wesleysimplicio/{component}",
        "package": component,
        "version": "1.2.3",
        "commit": "a" * 40,
        "tag": "v1.2.3",
        "artifacts": [{
            "registry": "test", "os": "source", "arch": "any",
            "digest": "sha256:" + "a" * 64, "size": 1, "signature": "sig",
        }],
        "compatibility": {},
        "breaking_change": False,
        "changelog": [{"version": "1.2.3", "notes": "test"}],
        "channel": "stable",
    }


def test_doctor_measures_valid_child_manifest(tmp_path: Path):
    root = tmp_path / "workspace"
    repo = root / "simplicio-mapper"
    manifest_dir = repo / ".simplicio"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "component-release.json").write_text(
        json.dumps(_manifest("simplicio-mapper")), encoding="utf-8"
    )

    report = doctor_release_train(workspace_root=root)
    mapper = next(item for item in report["children"] if item["component"] == "simplicio-mapper")
    assert mapper["status"] == "MEASURED"
    assert mapper["reason"] == "component_manifest_valid"
    assert report["child_manifest_coverage"]["measured"] == 1
    assert report["child_manifest_coverage"]["unverified"] == len(CHILD_BLOCKERS) - 1
    assert report["eight_repo_conformance"] == "UNVERIFIED"


def test_doctor_blocks_invalid_child_manifest(tmp_path: Path):
    repo = tmp_path / "simplicio-runtime"
    repo.mkdir()
    (repo / "component-release.json").write_text("{}", encoding="utf-8")

    report = doctor_release_train(workspace_root=tmp_path)
    runtime = next(item for item in report["children"] if item["component"] == "simplicio-runtime")
    assert runtime["status"] == "BLOCKED"
    assert runtime["reason"] == "invalid_component_manifest"
    assert report["child_manifest_coverage"]["blocked"] == 1
    assert report["closes_eight_repo_ac"] is False


def test_doctor_marks_missing_workspace_unverified(tmp_path: Path):
    report = doctor_release_train(workspace_root=tmp_path / "missing")
    assert report["child_manifest_coverage"]["status"] == "UNVERIFIED"
    assert report["child_manifest_coverage"]["unverified"] == len(CHILD_BLOCKERS)
    assert all(item["reason"] == "workspace_root_missing" for item in report["children"])
