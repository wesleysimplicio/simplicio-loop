#!/usr/bin/env python3
"""Fail-closed metadata and submodule integrity gate for the Prism bundle."""

from __future__ import annotations

import argparse
import configparser
import json
import re
import subprocess
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

SCHEMA = "simplicio.prism-integrity/v1"
MINIMUM_PYTHON = (3, 11)
DEPENDENCY_FLOORS = {
    "simplicio-cli": (0, 16, 3),
    "simplicio-mapper": (0, 19, 0),
    "simplicio-fast": (2, 0, 14),
}
BRANCH_POLICY = {
    "simplicio-mapper": "main",
    "simplicio-dev-cli": "main",
    "simplicio-fast": "master",
}
VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
DEPENDENCY_RE = re.compile(r"^([A-Za-z0-9_-]+)>=(\d+\.\d+(?:\.\d+)?)")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _version(value: str) -> tuple[int, int, int]:
    match = VERSION_RE.search(value)
    return (
        (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))
        if match
        else (0, 0, 0)
    )


def _check(
    rows: list[dict[str, Any]],
    name: str,
    ok: bool,
    reason_code: str,
    evidence: Any,
) -> None:
    rows.append(
        {
            "name": name,
            "ok": bool(ok),
            "reason_code": "OK" if ok else reason_code,
            "evidence": evidence,
        }
    )


def _gitlinks(repo: Path) -> dict[str, str]:
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return {}
    links: dict[str, str] = {}
    for line in result.stdout.splitlines():
        match = re.match(r"^160000 commit ([0-9a-f]{40})\t(.+)$", line)
        if match:
            links[match.group(2)] = match.group(1)
    return links


def evaluate(repo: str | Path) -> dict[str, Any]:
    root = Path(repo).resolve()
    checks: list[dict[str, Any]] = []
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        return {
            "schema": SCHEMA,
            "ok": False,
            "checks": [],
            "reason_codes": ["PYPROJECT_UNREADABLE"],
            "detail": str(exc),
        }

    requires_python = str(project.get("requires-python") or "")
    _check(
        checks,
        "python.floor",
        requires_python.startswith(">=")
        and _version(requires_python) >= MINIMUM_PYTHON + (0,),
        "PYTHON_FLOOR_DRIFT",
        requires_python,
    )
    classifiers = set(project.get("classifiers") or ())
    _check(
        checks,
        "python.classifiers",
        "Programming Language :: Python :: 3.11" in classifiers,
        "PYTHON_CLASSIFIER_DRIFT",
        sorted(item for item in classifiers if "Python" in item),
    )

    dependencies: dict[str, tuple[int, int, int]] = {}
    for dependency in project.get("dependencies") or ():
        match = DEPENDENCY_RE.match(str(dependency))
        if match:
            dependencies[match.group(1)] = _version(match.group(2))
    for name, floor in DEPENDENCY_FLOORS.items():
        _check(
            checks,
            f"dependency.{name}",
            dependencies.get(name, (0, 0, 0)) >= floor,
            "DEPENDENCY_FLOOR_DRIFT",
            ".".join(str(item) for item in dependencies.get(name, (0, 0, 0))),
        )

    canonical_version = str(project.get("version") or "")
    try:
        fallback_text = (root / "simplicio_loop" / "__init__.py").read_text(
            encoding="utf-8"
        )
    except OSError:
        fallback_text = ""
    fallbacks = set(
        re.findall(r'__version__\s*=\s*["\']([^"\']+)["\']', fallback_text)
    )
    _check(
        checks,
        "version.fallback",
        fallbacks == {canonical_version},
        "VERSION_SURFACE_DRIFT",
        {"canonical": canonical_version, "fallbacks": sorted(fallbacks)},
    )

    try:
        pins_document = json.loads(
            (root / "components" / "submodules.json").read_text(encoding="utf-8")
        )
        pins = pins_document["components"]
        parser = configparser.ConfigParser()
        parser.read(root / ".gitmodules", encoding="utf-8")
    except (OSError, KeyError, json.JSONDecodeError, configparser.Error) as exc:
        pins = {}
        parser = configparser.ConfigParser()
        _check(
            checks,
            "submodules.metadata",
            False,
            "SUBMODULE_METADATA_UNREADABLE",
            str(exc),
        )
    gitlinks = _gitlinks(root)
    for name, branch in BRANCH_POLICY.items():
        pin = pins.get(name) or {}
        path = str(pin.get("path") or "")
        section = f'submodule "{path}"'
        module = dict(parser.items(section)) if parser.has_section(section) else {}
        sha = str(pin.get("sha") or "")
        _check(
            checks,
            f"submodule.{name}",
            bool(path)
            and pin.get("ref") == branch
            and module.get("branch") == branch
            and module.get("shallow") == "true"
            and SHA_RE.fullmatch(sha) is not None
            and (not gitlinks or gitlinks.get(path) == sha),
            "SUBMODULE_PIN_DRIFT",
            {
                "path": path,
                "manifest_ref": pin.get("ref"),
                "gitmodules_branch": module.get("branch"),
                "manifest_sha": sha or None,
                "gitlink_sha": gitlinks.get(path),
            },
        )

    reason_codes = sorted(
        {row["reason_code"] for row in checks if not row["ok"]}
    )
    return {
        "schema": SCHEMA,
        "ok": not reason_codes,
        "python_minimum": "3.11",
        "checks": checks,
        "reason_codes": reason_codes,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = evaluate(args.repo)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"prism-integrity: {'READY' if report['ok'] else 'BLOCKED'}")
        for row in report["checks"]:
            marker = "✓" if row["ok"] else "✗"
            print(f"{marker} {row['name']}: {row['reason_code']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
