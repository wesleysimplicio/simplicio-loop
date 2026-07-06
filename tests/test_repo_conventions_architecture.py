"""Unit tests for `discover_architecture()` in `scripts/repo_conventions.py` — the mined
architecture/test-runner/lint-command signal that Step 1a'/Step 4 of simplicio-tasks use to make
generated code follow the REPO'S OWN structure and quality gates instead of a generic guess.

`repo_conventions.py selftest` already covers the empty-dir and Makefile cases; this file covers
the Node (package.json) path and ADR-glob discovery that the selftest doesn't reach.
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import repo_conventions  # noqa: E402


def test_package_json_test_and_lint_scripts_are_found(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "jest", "lint": "eslint ."}}), encoding="utf-8")
    arch = repo_conventions.discover_architecture(str(tmp_path))
    assert arch["test_runner"] == "npm run test"
    assert arch["lint_cmd"] == "npm run lint"


def test_package_json_without_matching_scripts_falls_back_to_none(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "tsc"}}),
                                            encoding="utf-8")
    arch = repo_conventions.discover_architecture(str(tmp_path))
    assert arch["test_runner"] is None
    assert arch["lint_cmd"] is None


def test_malformed_package_json_never_crashes(tmp_path):
    (tmp_path / "package.json").write_text("{not valid json", encoding="utf-8")
    arch = repo_conventions.discover_architecture(str(tmp_path))
    assert arch["test_runner"] is None


def test_adr_glob_is_discovered_under_specs_architecture(tmp_path):
    adr_dir = tmp_path / ".specs" / "architecture"
    adr_dir.mkdir(parents=True)
    (adr_dir / "ADR-001-use-postgres.md").write_text("# ADR-001\n", encoding="utf-8")
    (adr_dir / "ADR-002-event-bus.md").write_text("# ADR-002\n", encoding="utf-8")
    arch = repo_conventions.discover_architecture(str(tmp_path))
    assert ".specs/architecture/ADR-001-use-postgres.md" in arch["docs"]
    assert ".specs/architecture/ADR-002-event-bus.md" in arch["docs"]


def test_specs_readme_counts_as_an_architecture_doc(tmp_path):
    specs = tmp_path / ".specs"
    specs.mkdir()
    (specs / "README.md").write_text("# Specs map\n", encoding="utf-8")
    arch = repo_conventions.discover_architecture(str(tmp_path))
    assert ".specs/README.md" in arch["docs"]


def test_bare_makefile_with_no_test_or_lint_target_yields_none(tmp_path):
    (tmp_path / "Makefile").write_text("build:\n\tgo build ./...\n", encoding="utf-8")
    arch = repo_conventions.discover_architecture(str(tmp_path))
    assert arch["test_runner"] is None
    assert arch["lint_cmd"] is None


def test_scripts_check_py_is_preferred_test_runner_signal_when_present(tmp_path):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "check.py").write_text("", encoding="utf-8")
    arch = repo_conventions.discover_architecture(str(tmp_path))
    assert arch["test_runner"] == "python3 scripts/check.py"


def test_default_profile_carries_a_real_architecture_dict():
    profile = repo_conventions.default_profile()
    assert set(profile["architecture"].keys()) == {"docs", "test_runner", "lint_cmd"}


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_repo_conventions_architecture")
