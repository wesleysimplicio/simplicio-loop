from pathlib import Path
import tomllib


ROOT = Path(__file__).parents[1]


def test_loop_owns_an_explicit_dependency_boundary() -> None:
    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = manifest["project"]["dependencies"]
    names = {dependency.split(";", 1)[0].split("<", 1)[0].split(">", 1)[0].split("=", 1)[0].strip() for dependency in dependencies}
    assert "hermes-agent" not in names
    assert "simplicio-sprint" not in names
    assert "rich" not in names


def test_packaging_boundary_documentation_exists() -> None:
    text = (ROOT / "docs" / "packaging-environment.md").read_text(encoding="utf-8")
    assert "separate consumers" in text
    assert "pip check" in text
    assert "rich==14.3.3" in text
    assert "rich>=15.0.0" in text
