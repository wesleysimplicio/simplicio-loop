from __future__ import annotations

import shutil
from pathlib import Path

from scripts.prism_integrity import evaluate

REPO = Path(__file__).resolve().parents[1]


def fixture_repo(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    (target / "simplicio_loop").mkdir(parents=True)
    (target / "components").mkdir()
    for relative in (
        "pyproject.toml",
        ".gitmodules",
        "components/submodules.json",
        "simplicio_loop/__init__.py",
    ):
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / relative, destination)
    return target


def test_real_repository_metadata_and_pins_are_coherent():
    report = evaluate(REPO)
    assert report["ok"] is True, report
    assert report["python_minimum"] == "3.11"


def test_python_floor_drift_is_blocked(tmp_path):
    repo = fixture_repo(tmp_path)
    pyproject = repo / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            'requires-python = ">=3.11"',
            'requires-python = ">=3.8"',
        ),
        encoding="utf-8",
    )
    report = evaluate(repo)
    assert report["ok"] is False
    assert "PYTHON_FLOOR_DRIFT" in report["reason_codes"]


def test_dependency_version_and_fast_branch_drift_are_blocked(tmp_path):
    repo = fixture_repo(tmp_path)
    pyproject = repo / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            "simplicio-fast>=2.0.22",
            "simplicio-fast>=1.0.0",
        ),
        encoding="utf-8",
    )
    gitmodules = repo / ".gitmodules"
    gitmodules.write_text(
        gitmodules.read_text(encoding="utf-8").replace(
            "\tbranch = master",
            "\tbranch = main",
        ),
        encoding="utf-8",
    )
    report = evaluate(repo)
    assert "SUBMODULE_PIN_DRIFT" in report["reason_codes"]
    assert "SUBMODULE_PIN_DRIFT" in report["reason_codes"]


def test_version_fallback_drift_is_blocked(tmp_path):
    repo = fixture_repo(tmp_path)
    package = repo / "simplicio_loop" / "__init__.py"
    package.write_text(
        package.read_text(encoding="utf-8").replace("3.43.5", "9.9.9"),
        encoding="utf-8",
    )
    report = evaluate(repo)
    assert "VERSION_SURFACE_DRIFT" in report["reason_codes"]
