from __future__ import annotations

import json
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_active_release_train_contract_matches_project_version() -> None:
    contract = json.loads(
        (ROOT / "docs" / "release-train" / "compatibility-contract.json").read_text(encoding="utf-8")
    )
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    assert contract["schema"] == "simplicio.release-train.compatibility/v1"
    assert contract["release_train_version"] == f"v{version}"
    assert contract["evidence_policy"] == "measured manifests only; missing or invalid is blocked/unverified"


def test_active_contract_names_the_loop_train_components() -> None:
    contract = json.loads(
        (ROOT / "docs" / "release-train" / "compatibility-contract.json").read_text(encoding="utf-8")
    )
    assert {"loop", "mapper", "dev-cli", "fast"} <= set(contract["components"])
    assert set(contract["allowed_states"]) == {"MEASURED", "BLOCKED", "UNVERIFIED"}
