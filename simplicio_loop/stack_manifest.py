"""Single installed-stack manifest for Mapper, Fast, Dev CLI and Loop.

Release-train floors (#558) are derived from this package's ``pyproject.toml``
lower bounds when available so stack health does not drift from dependency pins.
"""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import json
import re
import shutil
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
    "simplicio-mapper": "0.26.26",
    "simplicio-fast": "2.0.28",
    "simplicio-cli": "0.18.10",
    "simplicio-loop": "3.43.4",
}

# These are operator identities, not distribution names.  ``simplicio-dev-cli``
# is the required mutation entrypoint exported by the ``simplicio-cli``
# distribution; it is intentionally checked separately from the distribution
# so a partial or transitive install cannot look like a complete Loop stack.
REQUIRED_OPERATOR_BINDINGS = (
    ("simplicio-mapper", "simplicio-mapper", "simplicio-mapper"),
    ("simplicio-dev-cli", "simplicio-cli", "simplicio-dev-cli"),
)

_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9_.-]+)")


def _normalize_distribution_name(name: str) -> str:
    """Normalize a Python distribution name according to PEP 503."""
    return re.sub(r"[-_.]+", "-", str(name).strip()).lower()


def _requirement_name(spec: str) -> str | None:
    match = _REQUIREMENT_NAME.match(str(spec))
    return _normalize_distribution_name(match.group(1)) if match else None


def _dependency_specs_from_pyproject(pyproject_text: str) -> dict[str, str]:
    """Return direct project dependencies keyed by normalized distribution name."""
    try:
        import tomllib

        project = tomllib.loads(pyproject_text).get("project", {})
        dependencies = project.get("dependencies", []) if isinstance(project, dict) else []
    except (ImportError, TypeError, ValueError):
        dependencies = []
    if not isinstance(dependencies, list):
        return {}
    result: dict[str, str] = {}
    for spec in dependencies:
        name = _requirement_name(str(spec))
        if name:
            result[name] = str(spec)
    return result


def _declared_dependency_specs() -> dict[str, str]:
    """Read Loop's direct dependencies from checkout or installed metadata.

    A wheel does not contain the repository's ``pyproject.toml``.  Falling back
    to ``Requires-Dist`` keeps the same validation available to users who run
    ``simplicio-loop-stack`` after a normal PyPI installation.
    """
    path = _pyproject_path()
    if path.is_file():
        try:
            return _dependency_specs_from_pyproject(path.read_text(encoding="utf-8"))
        except OSError:
            pass
    try:
        requirements = metadata.requires("simplicio-loop") or []
    except metadata.PackageNotFoundError:
        requirements = []
    return {
        name: str(spec)
        for spec in requirements
        if (name := _requirement_name(str(spec)))
        and "; extra ==" not in str(spec).lower()
    }


def _console_entrypoint_owners() -> dict[str, set[str]]:
    """Return console-script names grouped by their owning distribution."""
    owners: dict[str, set[str]] = {}
    try:
        entrypoints = metadata.entry_points(group="console_scripts")
    except TypeError:  # pragma: no cover - compatibility with older metadata APIs.
        entrypoints = metadata.entry_points().get("console_scripts", ())
    for entrypoint in entrypoints:
        distribution = getattr(entrypoint, "dist", None)
        distribution_name = getattr(distribution, "name", None)
        if not distribution_name:
            continue
        owners.setdefault(_normalize_distribution_name(distribution_name), set()).add(
            str(entrypoint.name)
        )
    return owners


def operator_bindings() -> list[dict[str, Any]]:
    """Verify the two mandatory Loop operators and their real entrypoints."""
    declared = _declared_dependency_specs()
    owners = _console_entrypoint_owners()
    bindings: list[dict[str, Any]] = []
    for operator, distribution, entrypoint in REQUIRED_OPERATOR_BINDINGS:
        normalized_distribution = _normalize_distribution_name(distribution)
        dependency_declared = normalized_distribution in declared
        installed = _installed_version(distribution)
        entrypoint_declared = entrypoint in owners.get(normalized_distribution, set())
        resolved = shutil.which(entrypoint) or ""
        if not dependency_declared:
            status = "dependency-not-declared"
        elif installed is None:
            status = "dependency-missing"
        elif not entrypoint_declared:
            status = "entrypoint-not-declared"
        elif not resolved:
            status = "entrypoint-not-on-path"
        else:
            status = "ok"
        bindings.append({
            "operator": operator,
            "distribution": distribution,
            "entrypoint": entrypoint,
            "dependency_declared": dependency_declared,
            "dependency_spec": declared.get(normalized_distribution, ""),
            "installed": installed,
            "entrypoint_declared": entrypoint_declared,
            "resolved": resolved,
            "status": status,
        })
    return bindings


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
    for name, spec in _dependency_specs_from_pyproject(pyproject_text).items():
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
    else:
        # Installed wheels carry dependency metadata, but not the checkout's
        # pyproject.  Use that metadata instead of retaining stale fallback
        # floors from an older release.
        for name, spec in _declared_dependency_specs().items():
            if name not in floors or name == "simplicio-loop":
                continue
            floor_match = re.search(r"(?:>=|==|~=)\s*([0-9A-Za-z.\-+]+)", spec)
            if floor_match:
                floors[name] = floor_match.group(1)
        installed_loop = _installed_version("simplicio-loop")
        if installed_loop:
            floors["simplicio-loop"] = installed_loop
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
    bindings = operator_bindings()
    binding_drift = [item["operator"] for item in bindings if item["status"] != "ok"]
    missing_or_drifted = list(dict.fromkeys([*drifted, *binding_drift]))
    return {
        "schema": STACK_SCHEMA,
        "version": 1,
        "package": "simplicio-loop",
        "components": components,
        "operator_bindings": bindings,
        "operator_contract_healthy": not binding_drift,
        "healthy": not missing_or_drifted,
        "missing_or_drifted": missing_or_drifted,
        "source": "pyproject.toml or installed Requires-Dist metadata",
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
        for binding in document["operator_bindings"]:
            print(
                f"- operator {binding['operator']}: {binding['status']} "
                f"({binding['distribution']} -> {binding['entrypoint']})"
            )
    return 0 if document["healthy"] or not args.check else 1


if __name__ == "__main__":
    sys.exit(main())
