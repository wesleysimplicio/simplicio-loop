import json
from pathlib import Path

from scripts import release_manifest

REPO = Path(__file__).resolve().parents[1]


def test_release_manifest_proves_all_surfaces_match():
    report = release_manifest.build_manifest(REPO)
    assert report["schema"] == "simplicio.release-manifest/v1"
    assert report["ready"] is True
    assert report["mismatches"] == []
    json.dumps(report)


def test_release_manifest_accepts_matching_fixture(tmp_path: Path):
    (tmp_path / "packaging" / "npm").mkdir(parents=True)
    (tmp_path / ".cursor-plugin").mkdir()
    (tmp_path / "simplicio_loop").mkdir()
    (tmp_path / "pyproject.toml").write_text('version = "1.2.3"\n')
    for path in (tmp_path / "packaging" / "npm" / "package.json",
                 tmp_path / ".cursor-plugin" / "plugin.json"):
        path.write_text(json.dumps({"version": "1.2.3"}))
    (tmp_path / "simplicio_loop" / "__init__.py").write_text(
        '__version__ = "1.2.3"\n__version__ = "1.2.3"\n')
    assert release_manifest.build_manifest(tmp_path, tag="v1.2.3")["ready"] is True


def test_release_manifest_rejects_wrong_tag():
    report = release_manifest.build_manifest(REPO, tag="v0.0.0")
    assert any("tag" in error for error in report["errors"])


def test_release_manifest_detects_manifest_drift(tmp_path: Path):
    (tmp_path / "packaging" / "npm").mkdir(parents=True)
    (tmp_path / ".cursor-plugin").mkdir()
    (tmp_path / "simplicio_loop").mkdir()
    (tmp_path / "pyproject.toml").write_text('version = "1.2.3"\n')
    (tmp_path / "packaging" / "npm" / "package.json").write_text('{"version":"1.2.4"}')
    (tmp_path / ".cursor-plugin" / "plugin.json").write_text('{"version":"1.2.3"}')
    (tmp_path / "simplicio_loop" / "__init__.py").write_text('__version__ = "1.2.3"\n')
    report = release_manifest.build_manifest(tmp_path)
    assert report["ready"] is False
    assert "npm" in report["mismatches"]