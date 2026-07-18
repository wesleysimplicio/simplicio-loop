#!/usr/bin/env python3
"""Deterministic repository test-infrastructure probe for issue #526."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "simplicio.test-infrastructure-probe/v1"
ECOSYSTEMS = ("dotnet", "node", "python", "go", "rust", "java")


def _files(root: Path, patterns: Iterable[str]) -> list[str]:
    found: set[str] = set()
    for pattern in patterns:
        found.update(path.relative_to(root).as_posix() for path in root.glob(pattern) if path.is_file())
    return sorted(found)


def _has_any(root: Path, patterns: Iterable[str]) -> bool:
    return bool(_files(root, patterns))


def _harness_status(harness: dict[str, Any] | None) -> dict[str, Any]:
    required = ("source", "log", "code_hash")
    if not harness:
        return {"status": "missing", "missing": list(required)}
    missing = [key for key in required if not str(harness.get(key, "")).strip()]
    if missing:
        return {"status": "invalid", "missing": missing}
    source_hash = hashlib.sha256(str(harness["source"]).encode("utf-8")).hexdigest()
    return {
        "status": "verified",
        "source_sha256": source_hash,
        "log": str(harness["log"]),
        "code_hash": str(harness["code_hash"]),
    }


def _dimension(status: str, reason: str, evidence: list[str] | None = None) -> dict[str, Any]:
    return {"status": status, "reason": reason, "evidence": evidence or []}


def probe(root: str | Path, *, external_harness: dict[str, Any] | None = None) -> dict[str, Any]:
    base = Path(root).resolve()
    if not base.is_dir():
        raise ValueError(f"repository root is not a directory: {base}")

    projects = {
        "dotnet": _files(base, ("**/*.csproj", "**/*.sln")),
        "node": _files(base, ("**/package.json", "**/package-lock.json", "**/pnpm-lock.yaml", "**/yarn.lock")),
        "python": _files(base, ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini")),
        "go": _files(base, ("go.mod", "**/*_test.go")),
        "rust": _files(base, ("Cargo.toml", "**/Cargo.lock")),
        "java": _files(base, ("pom.xml", "build.gradle", "build.gradle.kts", "**/src/test/**/*.java")),
    }
    test_files = {
        "dotnet": _files(base, ("**/*Tests*.csproj", "**/*Test*.csproj")),
        "node": _files(base, ("**/*.test.js", "**/*.test.ts", "**/*.spec.js", "**/*.spec.ts")),
        "python": _files(base, ("test_*.py", "*_test.py", "**/tests/test_*.py", "**/tests/*_test.py")),
        "go": _files(base, ("**/*_test.go",)),
        "rust": _files(base, ("**/tests/**/*.rs",)),
        "java": _files(base, ("**/src/test/**/*.java",)),
    }
    coverage_files = {
        "python": _files(base, (".coveragerc", "coverage.toml")),
        "node": _files(base, ("nyc.config.js", "c8.config.js")),
        "java": _files(base, ("**/jacoco*.xml",)),
        "rust": _files(base, ("tarpaulin.toml",)),
        "go": _files(base, (".gocov.yml",)),
        "dotnet": _files(base, ("coverlet.runsettings",)),
    }
    workflows = _files(base, (".github/workflows/*",))
    harness = _harness_status(external_harness)
    native_tests = sorted({item for values in test_files.values() for item in values})
    unit = (
        _dimension("verified", "native_test_files_detected", native_tests)
        if native_tests
        else (
            _dimension("verified", "external_harness_complete", ["external_harness"])
            if harness["status"] == "verified"
            else _dimension("pending", "no_tests_and_no_external_harness")
        )
    )
    coverage = (
        _dimension("verified", "coverage_configuration_detected", sorted({item for values in coverage_files.values() for item in values}))
        if any(coverage_files.values())
        else _dimension("waived:no-infra", "no_coverage_tooling_detected")
    )
    ci = (
        _dimension("verified", "github_actions_workflow_detected", workflows)
        if workflows
        else _dimension("waived:no-infra", "no_ci_workflow_detected")
    )
    dimensions = {"unit": unit, "coverage": coverage, "ci": ci}
    return {
        "schema": SCHEMA,
        "root": str(base),
        "ecosystems": [name for name in ECOSYSTEMS if projects[name]],
        "projects": projects,
        "test_files": test_files,
        "coverage_files": coverage_files,
        "ci": {"status": "verified" if workflows else "waived:no-infra", "evidence": workflows},
        "external_harness": harness,
        "dimensions": dimensions,
        "ready": not any(item["status"] == "pending" for item in dimensions.values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--external-harness", type=Path)
    args = parser.parse_args(argv)
    harness = json.loads(args.external_harness.read_text(encoding="utf-8")) if args.external_harness else None
    print(json.dumps(probe(args.root, external_harness=harness), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
