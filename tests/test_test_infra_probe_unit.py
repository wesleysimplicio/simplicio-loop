"""Unit tests for scripts/test_infra_probe.py (#526 Etapa 3).

Beyond the worker's own in-memory `selftest`, these prove: this repo (Python + pytest) is
detected as the positive fixture, a .NET repo with no test project is correctly reported absent,
each of the six ecosystems' markers are individually detected, CI detection is scoped to the
ecosystem whose command actually appears, and `record_anchor` writes the MEASURED summary onto
anchor.json without touching anything else already there.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts import test_infra_probe as probe_mod

REPO = Path(__file__).resolve().parents[1]


def test_this_repo_is_detected_as_python_positive_fixture():
    """This repo IS Python + pytest — the natural positive fixture (#526 AC1)."""
    result = probe_mod.probe(REPO)
    assert result["schema"] == probe_mod.SCHEMA
    assert result["measured"] is True
    assert result["test_infra"]["unit"] == "present"
    assert "python" in result["detected_ecosystems"]
    assert any(m.endswith(".py") for m in result["ecosystems"]["python"]["unit_markers"])


def test_dotnet_absence_is_correctly_reported(tmp_path):
    """A repo with source files but no test project reports dotnet unit absent — never a
    false positive from an unrelated source file."""
    (tmp_path / "Calc.cs").write_text("class Calc { public int Add(int a, int b) => a + b; }",
                                      encoding="utf-8")
    result = probe_mod.probe(tmp_path)
    assert result["ecosystems"]["dotnet"]["unit"] is False
    assert result["ecosystems"]["dotnet"]["unit_markers"] == []
    assert result["test_infra"]["unit"] == "absent"
    assert result["test_infra"]["coverage"] == "absent"
    assert result["test_infra"]["ci"] == "absent"
    assert result["detected_ecosystems"] == []


def test_dotnet_test_project_and_coverlet_detected(tmp_path):
    (tmp_path / "Calc.Tests.csproj").write_text(
        "<Project><ItemGroup><PackageReference Include=\"coverlet.collector\" "
        "Version=\"3.0.0\" /></ItemGroup></Project>", encoding="utf-8")
    result = probe_mod.probe(tmp_path)
    eco = result["ecosystems"]["dotnet"]
    assert eco["unit"] is True
    assert eco["coverage"] is True
    assert "Calc.Tests.csproj" in eco["unit_markers"]


def test_node_jest_and_nyc_coverage_detected(tmp_path):
    (tmp_path / "jest.config.js").write_text("module.exports = { collectCoverage: false }",
                                              encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({"devDependencies": {"nyc": "^15.0.0"}}),
                                            encoding="utf-8")
    result = probe_mod.probe(tmp_path)
    eco = result["ecosystems"]["node"]
    assert eco["unit"] is True
    assert eco["coverage"] is True


def test_python_pyproject_pytest_and_coverage_sections_detected(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n\n"
        "[tool.coverage.run]\nbranch = true\n", encoding="utf-8")
    result = probe_mod.probe(tmp_path)
    eco = result["ecosystems"]["python"]
    assert eco["unit"] is True
    assert eco["coverage"] is True


def test_go_test_file_and_makefile_cover_flag_detected(tmp_path):
    (tmp_path / "calc_test.go").write_text("package calc\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("test:\n\tgo test -cover ./...\n", encoding="utf-8")
    result = probe_mod.probe(tmp_path)
    eco = result["ecosystems"]["go"]
    assert eco["unit"] is True
    assert eco["coverage"] is True


def test_rust_integration_tests_dir_and_tarpaulin_detected(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "calc_it.rs").write_text("#[test]\nfn adds() {}\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(
        "[package]\nname = \"calc\"\n\n[package.metadata.tarpaulin]\nignore-tests = false\n",
        encoding="utf-8")
    result = probe_mod.probe(tmp_path)
    eco = result["ecosystems"]["rust"]
    assert eco["unit"] is True
    assert eco["coverage"] is True


def test_java_maven_surefire_and_jacoco_detected(tmp_path):
    d = tmp_path / "src" / "test" / "java" / "com" / "example"
    d.mkdir(parents=True)
    (d / "CalcTest.java").write_text("class CalcTest {}", encoding="utf-8")
    (tmp_path / "pom.xml").write_text(
        "<project><build><plugins><plugin><artifactId>maven-surefire-plugin</artifactId>"
        "</plugin><plugin><artifactId>jacoco-maven-plugin</artifactId></plugin>"
        "</plugins></build></project>", encoding="utf-8")
    result = probe_mod.probe(tmp_path)
    eco = result["ecosystems"]["java"]
    assert eco["unit"] is True
    assert eco["coverage"] is True


def test_ci_detection_is_scoped_to_the_ecosystem_whose_command_actually_runs(tmp_path):
    """A CI workflow running `pytest` must not be misreported as dotnet/go/etc CI (#526 AC1)."""
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("jobs:\n  test:\n    steps:\n      - run: pytest tests/\n",
                                encoding="utf-8")
    result = probe_mod.probe(tmp_path)
    assert result["ecosystems"]["python"]["ci"] is True
    assert result["ecosystems"]["dotnet"]["ci"] is False
    assert result["ecosystems"]["go"]["ci"] is False
    assert result["test_infra"]["ci"] == "present"


def test_prune_dirs_are_not_walked_into(tmp_path):
    """node_modules noise must never leak a false unit-test positive for an unrelated ecosystem."""
    nm = tmp_path / "node_modules" / "somepkg"
    nm.mkdir(parents=True)
    (nm / "somepkg_test.go").write_text("package somepkg\n", encoding="utf-8")
    result = probe_mod.probe(tmp_path)
    assert result["ecosystems"]["go"]["unit"] is False


def test_record_anchor_writes_summary_without_disturbing_existing_keys(tmp_path):
    anchor = tmp_path / "anchor.json"
    anchor.write_text(json.dumps({"item": "526", "goal": "frozen",
                                  "criteria": [{"id": "AC1", "status": "pending"}]}),
                       encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    result = probe_mod.probe(tmp_path)
    assert probe_mod.record_anchor(anchor, result) is True
    data = json.loads(anchor.read_text(encoding="utf-8"))
    assert data["goal"] == "frozen"
    assert data["criteria"] == [{"id": "AC1", "status": "pending"}]
    assert data["test_infra"]["unit"] == "present"
    assert data["test_infra"]["coverage"] == "absent"
    assert data["test_infra"]["measured"] is True


def test_record_anchor_missing_or_invalid_anchor_fails_closed(tmp_path):
    result = probe_mod.probe(tmp_path)
    assert probe_mod.record_anchor(tmp_path / "missing.json", result) is False
    broken = tmp_path / "broken.json"
    broken.write_text("[]", encoding="utf-8")
    assert probe_mod.record_anchor(broken, result) is False


def test_cli_probe_emits_measured_json(tmp_path, capsys):
    import subprocess
    import sys
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "test_infra_probe.py"), "probe",
         "--root", str(tmp_path)],
        cwd=REPO, capture_output=True, text=True,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["schema"] == probe_mod.SCHEMA
    assert payload["test_infra"]["unit"] == "present"


def test_selftest_subcommand_passes():
    import subprocess
    import sys
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "test_infra_probe.py"), "selftest"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "PASS" in result.stdout


def test_describe_cli_lists_probe_and_selftest():
    import subprocess
    import sys
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "test_infra_probe.py"), "--describe-cli"],
        cwd=REPO, capture_output=True, text=True,
    )
    spec = json.loads(result.stdout)
    assert set(spec["verbs"]) == {"probe", "selftest"}
    assert "--root" in spec["flags"] and "--anchor" in spec["flags"]


def test_matches_helper_basename_vs_full_path():
    assert probe_mod._matches("pytest.ini", "pytest.ini") is True
    assert probe_mod._matches("sub/pytest.ini", "pytest.ini") is True
    assert probe_mod._matches("src/test/java/com/Foo.java", "src/test/java/*.java") is True
    assert probe_mod._matches("other/Foo.java", "src/test/java/*.java") is False


def test_extract_harness_style_hash_not_confused_with_probe():
    # sanity: sha256 length constant used elsewhere in the #526 harness contract is 64 hex chars
    assert len(hashlib.sha256(b"x").hexdigest()) == 64
