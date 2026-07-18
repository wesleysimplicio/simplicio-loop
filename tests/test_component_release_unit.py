import json
from pathlib import Path

import pytest

from scripts import component_release as cr


VALID_COMPONENT = {
    "component": "simplicio-loop",
    "repo": "wesleysimplicio/simplicio-loop",
    "package": "simplicio-loop",
    "version": "3.38.0",
    "commit": "abc123",
    "tag": "v3.38.0",
    "artifacts": [{
        "registry": "pypi", "os": "any", "arch": "any",
        "digest": "sha256:" + "a" * 64, "size": 1024, "signature": "sig",
    }],
    "compatibility": {"simplicio-cli": ">=0.16.1"},
    "breaking_change": False,
    "changelog": [{"version": "3.38.0", "notes": "release"}],
    "channel": "stable",
}

VALID_ECOSYSTEM = {
    "release_id": 42,
    "components": {"simplicio-loop": {"version": "3.38.0", "commit": "abc", "digest": "sha256:x"}},
    "graph_hash": "sha256:graph",
    "contract_hashes": {"loop": "sha256:c"},
    "status": {"loop": "green"},
    "evidence": {"e2e": "pass"},
    "rollout": {"canary": "3.38.0"},
    "signature": "sig",
}


def test_valid_component_release_has_no_errors():
    assert cr.validate_component_release(VALID_COMPONENT) == []


def test_component_release_rejects_non_object():
    assert cr.validate_component_release("not-a-dict") != []
    assert cr.validate_component_release(None) != []


@pytest.mark.parametrize("missing", sorted(cr.COMPONENT_REQUIRED))
def test_component_release_rejects_missing_required_field(missing):
    data = dict(VALID_COMPONENT)
    del data[missing]
    errors = cr.validate_component_release(data)
    assert any("missing" in e for e in errors)


def test_component_release_rejects_unknown_field():
    data = dict(VALID_COMPONENT)
    data["not_in_the_schema"] = True
    errors = cr.validate_component_release(data)
    assert any("unknown" in e and "not_in_the_schema" in e for e in errors)


def test_component_release_rejects_bad_semver():
    data = dict(VALID_COMPONENT)
    data["version"] = "v3.38"
    errors = cr.validate_component_release(data)
    assert any("semantic" in e for e in errors)


def test_component_release_rejects_bad_channel():
    data = dict(VALID_COMPONENT)
    data["channel"] = "beta"
    errors = cr.validate_component_release(data)
    assert any("channel" in e for e in errors)


def test_component_release_rejects_bad_artifact_digest():
    data = json.loads(json.dumps(VALID_COMPONENT))
    data["artifacts"][0]["digest"] = "not-a-digest"
    errors = cr.validate_component_release(data)
    assert any("digest" in e for e in errors)


def test_component_release_rejects_non_bool_breaking_change():
    data = dict(VALID_COMPONENT)
    data["breaking_change"] = "yes"
    errors = cr.validate_component_release(data)
    assert any("breaking_change" in e for e in errors)


def test_component_release_rejects_bad_changelog_entry():
    data = json.loads(json.dumps(VALID_COMPONENT))
    data["changelog"][0]["version"] = "not-semver"
    errors = cr.validate_component_release(data)
    assert any("semantic" in e for e in errors)


def test_valid_ecosystem_release_has_no_errors():
    assert cr.validate_ecosystem_release(VALID_ECOSYSTEM) == []


def test_ecosystem_release_rejects_unknown_field():
    data = dict(VALID_ECOSYSTEM)
    data["surprise"] = 1
    errors = cr.validate_ecosystem_release(data)
    assert any("unknown" in e for e in errors)


@pytest.mark.parametrize("missing", sorted(cr.ECOSYSTEM_REQUIRED))
def test_ecosystem_release_rejects_missing_required_field(missing):
    data = dict(VALID_ECOSYSTEM)
    del data[missing]
    errors = cr.validate_ecosystem_release(data)
    assert any("missing" in e for e in errors)


def test_ecosystem_release_component_entry_requires_version_commit_digest():
    data = json.loads(json.dumps(VALID_ECOSYSTEM))
    del data["components"]["simplicio-loop"]["digest"]
    errors = cr.validate_ecosystem_release(data)
    assert any("digest" in e for e in errors)


def test_parse_dependency_spec():
    assert cr.parse_dependency_spec("simplicio-cli>=0.16.1") == ("simplicio-cli", [(">=", "0.16.1")])
    assert cr.parse_dependency_spec("foo>=1.0,<2.0") == ("foo", [(">=", "1.0"), ("<", "2.0")])


@pytest.mark.parametrize("installed,op,target,expected", [
    ("1.2.3", ">=", "1.2.3", True),
    ("1.2.2", ">=", "1.2.3", False),
    ("1.2.3", "==", "1.2.3", True),
    ("1.2.4", "==", "1.2.3", False),
    ("1.4.9", "~=", "1.4.2", True),
    ("1.5.0", "~=", "1.4.2", False),
])
def test_constraint_satisfied(installed, op, target, expected):
    assert cr.constraint_satisfied(installed, op, target) is expected


def test_check_dependency_drift_satisfied():
    row = cr.check_dependency_drift("simplicio-cli>=0.16.1", resolver=lambda name: "0.16.1")
    assert row["satisfied"] is True
    assert row["drift"] is None


def test_check_dependency_drift_out_of_range():
    row = cr.check_dependency_drift("simplicio-cli>=0.16.1", resolver=lambda name: "0.10.0")
    assert row["satisfied"] is False
    assert row["drift"] == "version_out_of_range"


def test_check_dependency_drift_not_installed():
    def _raise(name):
        raise cr.importlib_metadata.PackageNotFoundError(name)

    row = cr.check_dependency_drift("simplicio-cli>=0.16.1", resolver=_raise)
    assert row["installed"] is None
    assert row["drift"] == "not_installed"


def _write_fixture_repo(tmp_path: Path, version: str, deps: str) -> Path:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        f'[project]\nname = "fixture"\nversion = "{version}"\n'
        f'dependencies = [\n{deps}\n]\n'
    )
    return tmp_path


def test_doctor_report_clean_against_fixture(tmp_path):
    repo = _write_fixture_repo(tmp_path, "1.0.0", '  "widget>=2.0.0",')
    report = cr.build_doctor_report(repo, resolver=lambda name: "2.0.0")
    assert report["schema"] == cr.DOCTOR_SCHEMA
    assert report["declared_version"] == "1.0.0"
    assert report["clean"] is True
    assert report["drifted"] == []


def test_doctor_report_detects_drift_against_fixture(tmp_path):
    repo = _write_fixture_repo(tmp_path, "1.0.0", '  "widget>=2.0.0",')
    report = cr.build_doctor_report(repo, resolver=lambda name: "1.9.0")
    assert report["clean"] is False
    assert "widget" in report["drifted"]
    assert report["dependencies"][0]["drift"] == "version_out_of_range"


def test_doctor_report_flags_uninstalled_dependency(tmp_path):
    repo = _write_fixture_repo(tmp_path, "1.0.0", '  "missing-pkg>=1.0.0",')

    def _raise(name):
        raise cr.importlib_metadata.PackageNotFoundError(name)

    report = cr.build_doctor_report(repo, resolver=_raise)
    assert report["clean"] is False
    assert report["dependencies"][0]["drift"] == "not_installed"


def test_doctor_report_against_real_repo():
    report = cr.build_doctor_report(cr.REPO)
    assert report["schema"] == cr.DOCTOR_SCHEMA
    assert report["declared_version"]
    assert any(row["name"] == "simplicio-cli" for row in report["dependencies"])
    json.dumps(report)


def test_selftest_passes():
    assert cr.cmd_selftest({}) == 0
