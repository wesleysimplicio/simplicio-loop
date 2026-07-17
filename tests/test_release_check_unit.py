"""Unit tests for scripts/release_check.py — is a newer GitHub release available?

Covers semver parsing/comparison purely (no network) and the local-version reader against a
disposable pyproject.toml fixture.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_check", ROOT / "scripts" / "release_check.py")
release_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_check)  # type: ignore[union-attr]


def test_parse_semver_basic():
    assert release_check.parse_semver("3.38.0") == (3, 38, 0)


def test_parse_semver_strips_v_prefix():
    assert release_check.parse_semver("v3.38.0") == (3, 38, 0)


def test_parse_semver_malformed_sorts_lowest():
    assert release_check.parse_semver("not-a-version") == (-1, -1, -1)


def test_parse_semver_ignores_prerelease_suffix():
    assert release_check.parse_semver("3.38.0-rc1") == (3, 38, 0)


def test_compare_versions_behind():
    assert release_check.compare_versions("3.37.0", "3.38.0") == "behind"


def test_compare_versions_current():
    assert release_check.compare_versions("3.38.0", "3.38.0") == "current"


def test_compare_versions_ahead():
    assert release_check.compare_versions("3.39.0", "3.38.0") == "ahead"


def test_compare_versions_minor_version_ordering():
    assert release_check.compare_versions("3.9.0", "3.10.0") == "behind"


def test_compare_versions_malformed_remote_never_reads_as_behind():
    assert release_check.compare_versions("3.38.0", "garbage-tag") == "ahead"


def test_read_local_version_from_fixture(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\nversion = "9.9.9"\n', encoding="utf-8")
    assert release_check.read_local_version(str(tmp_path)) == "9.9.9"


def test_read_local_version_raises_on_missing_field(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\n', encoding="utf-8")
    try:
        release_check.read_local_version(str(tmp_path))
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_cmd_check_reports_unverified_when_gh_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(release_check, "_gh_latest_release_tag", lambda repo: None)
    rc = release_check.cmd_check({})
    out = capsys.readouterr().out
    assert "UNVERIFIED|" in out
    assert rc == 0


def test_cmd_check_json_reports_behind(monkeypatch, capsys):
    monkeypatch.setattr(release_check, "read_local_version", lambda repo=None: "3.0.0")
    monkeypatch.setattr(release_check, "_gh_latest_release_tag", lambda repo: "v3.1.0")
    rc = release_check.cmd_check({"json": True})
    out = capsys.readouterr().out
    assert '"comparison": "behind"' in out
    assert rc == 10


def test_cmd_check_returns_zero_when_current(monkeypatch, capsys):
    monkeypatch.setattr(release_check, "read_local_version", lambda repo=None: "3.1.0")
    monkeypatch.setattr(release_check, "_gh_latest_release_tag", lambda repo: "v3.1.0")
    rc = release_check.cmd_check({})
    out = capsys.readouterr().out
    assert "up to date" in out
    assert rc == 0


def test_selftest_passes():
    assert release_check.cmd_selftest({}) == 0
