import json
from pathlib import Path

import pytest

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


# --- Release-train schema validators (#558) ---------------------------------

GOOD_COMPONENT = {
    "component": "simplicio-loop",
    "repository": "wesleysimplicio/simplicio-loop",
    "package": "simplicio-loop",
    "version": "3.38.0",
    "artifacts": [
        {
            "os": "darwin",
            "arch": "arm64",
            "digest": "sha256:abc",
            "size": 1234,
            "signature": "sig:ed25519:xyz",
        }
    ],
    "compatibility_range": ">=0.16.1",
    "breaking_change": False,
    "changelog": "3.38.0: release train (#558)",
}

GOOD_ECOSYSTEM = {
    "release_id": "rel-2026-07-18-0001",
    "components": {"simplicio-loop": {"version": "3.38.0", "commit": "deadbeef", "digest": "sha256:abc"}},
    "graph_hash": "cc2ca200d5cda1fcd4475af1e9083399f5a53cb2fa724dbd6e15019790baa07d",
    "contract_hashes": {"simplicio.component-release/v1": "sha256:eee"},
    "status": {"darwin/arm64": "green"},
    "evidence": [{"kind": "conformance", "path": "x"}],
    "rollout": {"channel": "stable", "canary_pct": 0, "rollback": False},
}


def test_validate_component_release_accepts_good():
    ok, errs = release_manifest.validate_component_release(GOOD_COMPONENT)
    assert ok is True, errs
    assert errs == []


def test_validate_component_release_rejects_missing_keys():
    bad = dict(GOOD_COMPONENT)
    del bad["changelog"]
    del bad["breaking_change"]
    ok, errs = release_manifest.validate_component_release(bad)
    assert ok is False
    assert any("missing required keys" in e for e in errs)


def test_validate_component_release_rejects_bad_version():
    bad = dict(GOOD_COMPONENT)
    bad["version"] = "not-semver"
    ok, errs = release_manifest.validate_component_release(bad)
    assert ok is False
    assert any("version must be semantic" in e for e in errs)


def test_validate_component_release_rejects_bad_artifact():
    bad = dict(GOOD_COMPONENT)
    bad["artifacts"] = [{"digest": "", "size": -1, "signature": ""}]
    ok, errs = release_manifest.validate_component_release(bad)
    assert ok is False
    assert any("artifacts[0]" in e for e in errs)


def test_validate_ecosystem_release_accepts_good():
    ok, errs = release_manifest.validate_ecosystem_release(GOOD_ECOSYSTEM)
    assert ok is True, errs
    assert errs == []


def test_validate_ecosystem_release_rejects_missing_keys():
    bad = dict(GOOD_ECOSYSTEM)
    del bad["components"]
    del bad["graph_hash"]
    ok, errs = release_manifest.validate_ecosystem_release(bad)
    assert ok is False
    assert any("missing required keys" in e for e in errs)


def test_validate_ecosystem_release_rejects_empty_components():
    bad = dict(GOOD_ECOSYSTEM)
    bad["components"] = {}
    ok, errs = release_manifest.validate_ecosystem_release(bad)
    assert ok is False
    assert any("components must be a non-empty dict" in e for e in errs)


# --- release_train_check unit coverage (process-local, counts for coverage) ---

def test_release_train_check_unit_good_repo(tmp_path: Path):
    import shutil
    # copy a consistent fixture tree (pyproject/npm/plugin matching)
    (tmp_path / "packaging" / "npm").mkdir(parents=True)
    (tmp_path / ".cursor-plugin").mkdir()
    (tmp_path / "simplicio_loop").mkdir()
    (tmp_path / "pyproject.toml").write_text('version = "3.38.0"\n')
    (tmp_path / "packaging" / "npm" / "package.json").write_text('{"version":"3.38.0"}')
    (tmp_path / ".cursor-plugin" / "plugin.json").write_text('{"version":"3.38.0"}')
    (tmp_path / "simplicio_loop" / "__init__.py").write_text('__version__ = "3.38.0"\n')
    fx = tmp_path / "tests" / "fixtures" / "release_train"
    fx.mkdir(parents=True)
    (fx / "component_release_ok.json").write_text(json.dumps(GOOD_COMPONENT))
    (fx / "ecosystem_release_ok.json").write_text(json.dumps(GOOD_ECOSYSTEM))
    rc = release_manifest.release_train_check(str(tmp_path))
    assert rc == 0


def test_release_train_check_unit_bad_component_fixture(tmp_path: Path):
    fx = tmp_path / "tests" / "fixtures" / "release_train"
    fx.mkdir(parents=True)
    (fx / "component_release_ok.json").write_text(
        json.dumps({"component": "x", "version": "not-semver"})
    )
    rc = release_manifest.release_train_check(str(tmp_path))
    assert rc == 1


def test_release_train_check_unit_unreadable_fixture(tmp_path: Path):
    fx = tmp_path / "tests" / "fixtures" / "release_train"
    fx.mkdir(parents=True)
    # directory where a file is expected -> json.loads fails on read of a dir path
    (fx / "component_release_ok.json").mkdir()
    rc = release_manifest.release_train_check(str(tmp_path))
    assert rc == 1


def test_build_manifest_missing_version(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text('name = "x"\n')
    with pytest.raises(ValueError):
        release_manifest._pyproject_version(tmp_path / "pyproject.toml")


def test_build_manifest_json_version_errors(tmp_path: Path):
    bad = tmp_path / "package.json"
    bad.write_text("not json")
    with pytest.raises(ValueError):
        release_manifest._json_version(bad)


def test_build_manifest_fallback_versions(tmp_path: Path):
    init = tmp_path / "__init__.py"
    init.write_text('__version__ = "9.9.9"\nX = 1\n')
    vs = release_manifest._fallback_versions(init)
    assert vs == ["9.9.9"]


def test_pyproject_version_oserror(tmp_path: Path):
    missing = tmp_path / "nope.toml"
    with pytest.raises(OSError):
        release_manifest._pyproject_version(missing)


def test_validate_component_release_bad_scalars():
    bad = dict(GOOD_COMPONENT)
    bad["breaking_change"] = "yes"          # must be bool
    bad["compatibility_range"] = ""          # must be non-empty str
    bad["changelog"] = 123                  # must be non-empty str
    ok, errs = release_manifest.validate_component_release(bad)
    assert ok is False
    assert any("breaking_change must be a bool" in e for e in errs)
    assert any("compatibility_range must be a non-empty str" in e for e in errs)
    assert any("changelog must be a non-empty str" in e for e in errs)


def test_validate_component_release_non_dict_artifact():
    bad = dict(GOOD_COMPONENT)
    bad["artifacts"] = ["not-a-dict"]
    ok, errs = release_manifest.validate_component_release(bad)
    assert ok is False
    assert any("artifacts[0] must be an object" in e for e in errs)


def test_release_train_check_unit_manifest_not_ready(tmp_path: Path):
    # schemas valid but local manifest drift -> exit 1, manifest.ready False
    fx = tmp_path / "tests" / "fixtures" / "release_train"
    fx.mkdir(parents=True)
    (fx / "component_release_ok.json").write_text(json.dumps(GOOD_COMPONENT))
    (fx / "ecosystem_release_ok.json").write_text(json.dumps(GOOD_ECOSYSTEM))
    # local repo with mismatched versions
    (tmp_path / "packaging" / "npm").mkdir(parents=True)
    (tmp_path / ".cursor-plugin").mkdir()
    (tmp_path / "simplicio_loop").mkdir()
    (tmp_path / "pyproject.toml").write_text('version = "1.0.0"\n')
    (tmp_path / "packaging" / "npm" / "package.json").write_text('{"version":"1.0.1"}')
    (tmp_path / ".cursor-plugin" / "plugin.json").write_text('{"version":"1.0.0"}')
    (tmp_path / "simplicio_loop" / "__init__.py").write_text('__version__ = "1.0.0"\n')
    rc = release_manifest.release_train_check(str(tmp_path))
    assert rc == 1


def test_json_version_oserror(tmp_path: Path):
    missing = tmp_path / "nope.json"
    with pytest.raises(ValueError):
        release_manifest._json_version(missing)


def test_validate_component_release_negative_size(tmp_path: Path):
    bad = dict(GOOD_COMPONENT)
    bad["artifacts"] = [{"digest": "d", "size": -5, "signature": "s"}]
    ok, errs = release_manifest.validate_component_release(bad)
    assert ok is False
    assert any("size must be int>=0" in e for e in errs)