from __future__ import annotations

import tomllib
from pathlib import Path

from simplicio_loop import stack_manifest as manifest


ROOT = Path(__file__).parents[1]


def test_loop_declares_both_required_operator_distributions_directly() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dependencies = manifest._dependency_specs_from_pyproject(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert "simplicio-mapper" in dependencies
    assert "simplicio-cli" in dependencies
    assert "simplicio-dev-cli" not in {
        str(spec).split(";", 1)[0].split(">", 1)[0].split("<", 1)[0].strip()
        for spec in project["dependencies"]
    }


def test_stack_manifest_proves_operator_distribution_and_entrypoint_ownership(monkeypatch) -> None:
    monkeypatch.setattr(
        manifest,
        "_declared_dependency_specs",
        lambda: {"simplicio-mapper": "simplicio-mapper>=1.0", "simplicio-cli": "simplicio-cli>=1.0"},
    )
    monkeypatch.setattr(manifest, "_installed_version", lambda _name: "1.0.0")
    monkeypatch.setattr(
        manifest,
        "_console_entrypoint_owners",
        lambda: {
            "simplicio-mapper": {"simplicio-mapper"},
            "simplicio-cli": {"simplicio-dev-cli"},
        },
    )
    monkeypatch.setattr(manifest.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(manifest, "_train_components", lambda: [("simplicio-loop", "run", "1.0.0")])

    report = manifest.stack_manifest()

    assert report["healthy"] is True
    assert report["operator_contract_healthy"] is True
    bindings = {item["operator"]: item for item in report["operator_bindings"]}
    assert bindings["simplicio-mapper"]["status"] == "ok"
    assert bindings["simplicio-dev-cli"]["distribution"] == "simplicio-cli"
    assert bindings["simplicio-dev-cli"]["entrypoint_declared"] is True


def test_stack_manifest_fails_when_mapper_dependency_or_dev_cli_entrypoint_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(manifest, "_declared_dependency_specs", lambda: {"simplicio-cli": "simplicio-cli>=1.0"})
    monkeypatch.setattr(manifest, "_installed_version", lambda _name: "1.0.0")
    monkeypatch.setattr(
        manifest,
        "_console_entrypoint_owners",
        lambda: {"simplicio-cli": set()},
    )
    monkeypatch.setattr(manifest.shutil, "which", lambda _name: None)
    monkeypatch.setattr(manifest, "_train_components", lambda: [("simplicio-loop", "run", "1.0.0")])

    report = manifest.stack_manifest()

    bindings = {item["operator"]: item for item in report["operator_bindings"]}
    assert report["healthy"] is False
    assert report["operator_contract_healthy"] is False
    assert bindings["simplicio-mapper"]["status"] == "dependency-not-declared"
    assert bindings["simplicio-dev-cli"]["status"] == "entrypoint-not-declared"
    assert set(report["missing_or_drifted"]) == {"simplicio-mapper", "simplicio-dev-cli"}
