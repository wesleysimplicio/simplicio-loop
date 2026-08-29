"""Single installed-stack manifest for Mapper, Fast, Dev CLI and Loop.

Release-train floors (#558) are derived from this package's ``pyproject.toml``
lower bounds when available so stack health does not drift from dependency pins.
"""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import json
import re
import sys
from pathlib import Path
from typing import Any

STACK_SCHEMA = "simplicio.loop-stack/v1"

# Role labels are fixed; expected floors come from pyproject (see _train_components).
_COMPONENT_ROLES = (
    ("simplicio-mapper", "understand"),
    ("simplicio-fast", "search"),
    ("simplicio-cli", "change,verify"),
    ("simplicio-loop", "run"),
)

# Fallback floors when pyproject cannot be read (offline wheel / missing checkout).
_FALLBACK_FLOORS = {
    "simplicio-mapper": "0.26.20",
    "simplicio-fast": "2.0.28",
    "simplicio-cli": "0.18.10",
    "simplicio-loop": "3.43.2",
}


def _installed_version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def _version_tuple(version: str) -> tuple[int, int, int] | None:
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", version.strip())
    if not match:
        return None
    return (
        int(match.group(1)),
        int(match.group(2) or 0),
        int(match.group(3) or 0),
    )


def _pyproject_path() -> Path:
    return Path(__file__).resolve().parent.parent / "pyproject.toml"


def _parse_train_floors(pyproject_text: str) -> dict[str, str]:
    """Extract lower-bound floors for train packages + package version for loop."""
    floors = dict(_FALLBACK_FLOORS)
    version_match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject_text)
    if version_match:
        floors["simplicio-loop"] = version_match.group(1)
    deps_match = re.search(r"(?m)^dependencies\s*=\s*\[(.*?)\]", pyproject_text, re.S)
    if not deps_match:
        return floors
    for spec in re.findall(r'"([^"]+)"', deps_match.group(1)):
        name_match = re.match(r"^([A-Za-z0-9_.\-]+)", spec.strip())
        if not name_match:
            continue
        name = name_match.group(1)
        if name not in floors or name == "simplicio-loop":
            continue
        # Prefer >= / == / ~= lower bound as the train floor.
        floor_match = re.search(r"(?:>=|==|~=)\s*([0-9A-Za-z.\-+]+)", spec)
        if floor_match:
            floors[name] = floor_match.group(1)
    return floors


def _train_components() -> list[tuple[str, str, str]]:
    floors = dict(_FALLBACK_FLOORS)
    path = _pyproject_path()
    if path.is_file():
        try:
            floors = _parse_train_floors(path.read_text(encoding="utf-8"))
        except OSError:
            pass
    return [(name, role, floors[name]) for name, role in _COMPONENT_ROLES]


# Back-compat for tests/imports that still read COMPONENTS as a static tuple.
COMPONENTS = tuple(_train_components())


def stack_manifest() -> dict[str, Any]:
    components = []
    drifted = []
    for distribution, role, expected in _train_components():
        installed = _installed_version(distribution)
        # Healthy when installed meets the train floor (at floor or newer patch/minor).
        status = "ok"
        if installed is None:
            status = "missing-or-drifted"
        elif installed != expected:
            inst_t = _version_tuple(installed)
            exp_t = _version_tuple(expected)
            if inst_t is None or exp_t is None or inst_t < exp_t:
                status = "missing-or-drifted"
        components.append({
            "distribution": distribution,
            "role": role,
            "expected": expected,
            "installed": installed,
            "status": status,
        })
        if status != "ok":
            drifted.append(distribution)
    return {
        "schema": STACK_SCHEMA,
        "version": 1,
        "package": "simplicio-loop",
        "components": components,
        "healthy": not drifted,
        "missing_or_drifted": drifted,
        "source": "pyproject.toml",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect the installed Simplicio Loop stack")
    parser.add_argument("--json", action="store_true", help="emit the machine-readable manifest")
    parser.add_argument("--check", action="store_true", help="exit non-zero when the stack drifts")
    args = parser.parse_args(argv)
    document = stack_manifest()
    if args.json or args.check:
        print(json.dumps(document, ensure_ascii=False, sort_keys=True))
    else:
        print("simplicio-loop stack: " + ("healthy" if document["healthy"] else "drifted"))
        for component in document["components"]:
            print(f"- {component['distribution']}: {component['installed'] or 'missing'} ({component['role']})")
    return 0 if document["healthy"] or not args.check else 1


if __name__ == "__main__":
    sys.exit(main())
