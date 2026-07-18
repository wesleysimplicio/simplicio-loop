from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.test_infra_probe import probe


def test_detects_all_supported_ecosystems(tmp_path: Path) -> None:
    for name in ("dotnet.csproj", "package.json", "pyproject.toml", "go.mod", "Cargo.toml", "pom.xml"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    result = probe(tmp_path)
    assert result["ecosystems"] == ["dotnet", "node", "python", "go", "rust", "java"]


def test_native_tests_and_ci_are_measured(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest]", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_example.py").write_text("def test_ok(): pass", encoding="utf-8")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("name: ci", encoding="utf-8")
    result = probe(tmp_path)
    assert result["dimensions"]["unit"]["status"] == "verified"
    assert result["dimensions"]["ci"]["status"] == "verified"
    assert result["ready"] is True


def test_dotnet_without_tests_is_ready_only_with_complete_external_harness(tmp_path: Path) -> None:
    (tmp_path / "App.csproj").write_text("<Project />", encoding="utf-8")
    pending = probe(tmp_path)
    assert pending["dimensions"]["unit"]["status"] == "pending"
    assert pending["ready"] is False
    ready = probe(
        tmp_path,
        external_harness={"source": "case-1 PASS", "log": "8 PASS", "code_hash": "abc123"},
    )
    assert ready["dimensions"]["unit"]["reason"] == "external_harness_complete"
    assert ready["dimensions"]["coverage"]["status"] == "waived:no-infra"
    assert ready["ready"] is True


def test_incomplete_harness_does_not_silently_waive_unit(tmp_path: Path) -> None:
    result = probe(tmp_path, external_harness={"source": "only source"})
    assert result["external_harness"]["status"] == "invalid"
    assert result["dimensions"]["unit"]["status"] == "pending"


def test_invalid_root_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        probe(tmp_path / "missing")


def test_cli_emits_json(tmp_path: Path, capsys) -> None:
    from scripts import test_infra_probe

    assert test_infra_probe.main([str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == "simplicio.test-infrastructure-probe/v1"
