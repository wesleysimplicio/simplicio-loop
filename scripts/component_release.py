#!/usr/bin/env python3
"""simplicio.component-release/v1 + simplicio.ecosystem-release/v1 manifest schemas (issue #558).

Issue #558 is a P0 cross-repo/cross-registry release-train epic spanning eight repositories
(simplicio-mapper, simplicio-dev-cli/simplicio-cli, simplicio-loop, simplicio-runtime,
simplicio-agent, simplicio-code, simplicio-loop-oss, simplicio-loop-marketing) that this
checkout has no access to. What ships here is the in-repo slice that is actually reachable
from simplicio-loop alone:

    1. A real, tested validator for the two manifest schemas the issue proposes
       (`simplicio.component-release/v1`, `simplicio.ecosystem-release/v1`), so any producer
       (this repo included) can prove its manifest is well-formed before publishing it.
    2. A `doctor` command that reports THIS package's own declared dependency constraints
       (parsed from pyproject.toml) against what is actually installed
       (importlib.metadata), flagging drift for simplicio-loop's own direct dependencies.

This is NOT the automated cross-repo release train described in the issue (dependency-graph
generation from real manifests, bump-PR bots, canary/stable promotion, rollback-by-composition,
SLO-timed propagation across all eight repos) — none of that is buildable from a single-repo
checkout. See the module docstring of each command below and the issue for the full scope.

Verbs:
    validate-component   Validate a `simplicio.component-release/v1` JSON manifest file.
    validate-ecosystem   Validate a `simplicio.ecosystem-release/v1` JSON manifest file.
    doctor               Compare this repo's declared dependency constraints (pyproject.toml)
                          against what importlib.metadata reports as installed.
    selftest             Prove the validators + doctor logic against fixtures — no network.

Usage:
    python3 scripts/component_release.py validate-component manifest.json
    python3 scripts/component_release.py validate-ecosystem manifest.json
    python3 scripts/component_release.py doctor --json
    python3 scripts/component_release.py selftest
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from importlib import metadata as importlib_metadata
except ImportError:  # pragma: no cover - py<3.8 not supported by this repo anyway
    import importlib_metadata  # type: ignore

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

COMPONENT_SCHEMA = "simplicio.component-release/v1"
ECOSYSTEM_SCHEMA = "simplicio.ecosystem-release/v1"
DOCTOR_SCHEMA = "simplicio.release-doctor/v1"

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
DIGEST_RE = re.compile(r"^[a-z0-9]+:[0-9a-fA-F]+$")
VALID_CHANNELS = ("canary", "stable")

COMPONENT_REQUIRED = {
    "component", "repo", "package", "version", "commit", "tag",
    "artifacts", "compatibility", "breaking_change", "changelog", "channel",
}
COMPONENT_OPTIONAL = {"protocols", "migrations", "conformance_fixtures"}
COMPONENT_ALLOWED = COMPONENT_REQUIRED | COMPONENT_OPTIONAL

ARTIFACT_REQUIRED = {"registry", "os", "arch", "digest", "size", "signature"}
ARTIFACT_OPTIONAL = {"sbom", "provenance"}
ARTIFACT_ALLOWED = ARTIFACT_REQUIRED | ARTIFACT_OPTIONAL

CHANGELOG_REQUIRED = {"version", "notes"}
CHANGELOG_OPTIONAL = {"date"}
CHANGELOG_ALLOWED = CHANGELOG_REQUIRED | CHANGELOG_OPTIONAL

ECOSYSTEM_REQUIRED = {
    "release_id", "components", "graph_hash", "contract_hashes",
    "status", "evidence", "rollout", "signature",
}
ECOSYSTEM_OPTIONAL = {"provenance"}
ECOSYSTEM_ALLOWED = ECOSYSTEM_REQUIRED | ECOSYSTEM_OPTIONAL

COMPONENT_VERSION_REQUIRED = {"version", "commit", "digest"}


def _check_unknown(data: Dict[str, Any], allowed: set, where: str, errors: List[str]) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        errors.append(f"{where}: unknown field(s): {unknown}")


def _check_missing(data: Dict[str, Any], required: set, where: str, errors: List[str]) -> None:
    missing = sorted(required - set(data))
    if missing:
        errors.append(f"{where}: missing required field(s): {missing}")


def _validate_artifact(artifact: Any, index: int, errors: List[str]) -> None:
    where = f"artifacts[{index}]"
    if not isinstance(artifact, dict):
        errors.append(f"{where}: must be an object")
        return
    _check_unknown(artifact, ARTIFACT_ALLOWED, where, errors)
    _check_missing(artifact, ARTIFACT_REQUIRED, where, errors)
    digest = artifact.get("digest")
    if isinstance(digest, str) and digest and not DIGEST_RE.match(digest):
        errors.append(f"{where}: digest must look like '<algo>:<hex>' (got {digest!r})")
    size = artifact.get("size")
    if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0):
        errors.append(f"{where}: size must be a non-negative integer")
    signature = artifact.get("signature")
    if isinstance(signature, str) and not signature.strip():
        errors.append(f"{where}: signature must not be empty")


def _validate_changelog_entry(entry: Any, index: int, errors: List[str]) -> None:
    where = f"changelog[{index}]"
    if not isinstance(entry, dict):
        errors.append(f"{where}: must be an object")
        return
    _check_unknown(entry, CHANGELOG_ALLOWED, where, errors)
    _check_missing(entry, CHANGELOG_REQUIRED, where, errors)
    version = entry.get("version")
    if isinstance(version, str) and version and not SEMVER_RE.match(version):
        errors.append(f"{where}: version must be semantic MAJOR.MINOR.PATCH (got {version!r})")


def validate_component_release(data: Any) -> List[str]:
    """Validate a `simplicio.component-release/v1` manifest. Returns a list of errors
    (empty means valid). Never raises on malformed input."""
    errors: List[str] = []
    if not isinstance(data, dict):
        return ["manifest must be a JSON object"]
    _check_unknown(data, COMPONENT_ALLOWED, "manifest", errors)
    _check_missing(data, COMPONENT_REQUIRED, "manifest", errors)

    version = data.get("version")
    if isinstance(version, str) and version and not SEMVER_RE.match(version):
        errors.append(f"version must be semantic MAJOR.MINOR.PATCH (got {version!r})")

    for field in ("component", "repo", "package", "commit", "tag"):
        value = data.get(field)
        if field in data and (not isinstance(value, str) or not value.strip()):
            errors.append(f"{field} must be a non-empty string")

    artifacts = data.get("artifacts")
    if "artifacts" in data:
        if not isinstance(artifacts, list) or not artifacts:
            errors.append("artifacts must be a non-empty list")
        else:
            for i, artifact in enumerate(artifacts):
                _validate_artifact(artifact, i, errors)

    compatibility = data.get("compatibility")
    if "compatibility" in data:
        if not isinstance(compatibility, dict):
            errors.append("compatibility must be an object mapping component -> range")
        else:
            for name, rng in compatibility.items():
                if not isinstance(rng, str) or not rng.strip():
                    errors.append(f"compatibility[{name!r}] must be a non-empty range string")

    breaking_change = data.get("breaking_change")
    if "breaking_change" in data and not isinstance(breaking_change, bool):
        errors.append("breaking_change must be a boolean")

    changelog = data.get("changelog")
    if "changelog" in data:
        if not isinstance(changelog, list):
            errors.append("changelog must be a list")
        else:
            for i, entry in enumerate(changelog):
                _validate_changelog_entry(entry, i, errors)

    channel = data.get("channel")
    if "channel" in data and channel not in VALID_CHANNELS:
        errors.append(f"channel must be one of {VALID_CHANNELS} (got {channel!r})")

    protocols = data.get("protocols")
    if protocols is not None and not isinstance(protocols, dict):
        errors.append("protocols must be an object mapping schema/protocol name -> version")

    migrations = data.get("migrations")
    if migrations is not None and not isinstance(migrations, list):
        errors.append("migrations must be a list")

    fixtures = data.get("conformance_fixtures")
    if fixtures is not None and not isinstance(fixtures, list):
        errors.append("conformance_fixtures must be a list")

    return errors


def validate_ecosystem_release(data: Any) -> List[str]:
    """Validate a `simplicio.ecosystem-release/v1` manifest. Returns a list of errors
    (empty means valid). Never raises on malformed input."""
    errors: List[str] = []
    if not isinstance(data, dict):
        return ["manifest must be a JSON object"]
    _check_unknown(data, ECOSYSTEM_ALLOWED, "manifest", errors)
    _check_missing(data, ECOSYSTEM_REQUIRED, "manifest", errors)

    release_id = data.get("release_id")
    if "release_id" in data and not isinstance(release_id, (int, str)):
        errors.append("release_id must be an int or string (monotonic)")

    components = data.get("components")
    if "components" in data:
        if not isinstance(components, dict) or not components:
            errors.append("components must be a non-empty object mapping component -> version info")
        else:
            for name, info in components.items():
                where = f"components[{name!r}]"
                if not isinstance(info, dict):
                    errors.append(f"{where}: must be an object")
                    continue
                missing = sorted(COMPONENT_VERSION_REQUIRED - set(info))
                if missing:
                    errors.append(f"{where}: missing required field(s): {missing}")

    for field in ("graph_hash", "signature"):
        value = data.get(field)
        if field in data and (not isinstance(value, str) or not value.strip()):
            errors.append(f"{field} must be a non-empty string")

    for field in ("contract_hashes", "status", "evidence", "rollout"):
        value = data.get(field)
        if field in data and not isinstance(value, dict):
            errors.append(f"{field} must be an object")

    return errors


def _pyproject_text(repo: Path) -> str:
    return (repo / "pyproject.toml").read_text(encoding="utf-8")


def read_declared_version(repo: Path = REPO) -> str:
    text = _pyproject_text(repo)
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    if not match:
        raise ValueError("pyproject.toml has no project version")
    return match.group(1)


def read_declared_dependencies(repo: Path = REPO) -> List[str]:
    text = _pyproject_text(repo)
    match = re.search(r"(?m)^dependencies\s*=\s*\[(.*?)\]", text, re.S)
    if not match:
        return []
    return re.findall(r'"([^"]+)"', match.group(1))


_SPEC_TOKEN_RE = re.compile(r"(==|!=|>=|<=|~=|>|<)\s*([0-9A-Za-z.\-+]+)")


def parse_dependency_spec(spec: str) -> Tuple[str, List[Tuple[str, str]]]:
    """'simplicio-cli>=0.16.1' -> ('simplicio-cli', [('>=', '0.16.1')])."""
    name_match = re.match(r"^([A-Za-z0-9_.\-]+)", spec.strip())
    name = name_match.group(1) if name_match else spec.strip()
    rest = spec[len(name):].split(";", 1)[0]
    constraints = _SPEC_TOKEN_RE.findall(rest)
    return name, constraints


def _semver_tuple(version: str) -> Optional[Tuple[int, int, int]]:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version.strip())
    if not match:
        return None
    return tuple(int(x) for x in match.groups())  # type: ignore[return-value]


def constraint_satisfied(installed_version: str, op: str, target: str) -> Optional[bool]:
    """None means the comparison could not be evaluated (non-semver installed/target)."""
    installed = _semver_tuple(installed_version)
    wanted = _semver_tuple(target)
    if installed is None or wanted is None:
        return None
    if op == "==":
        return installed == wanted
    if op == "!=":
        return installed != wanted
    if op == ">=":
        return installed >= wanted
    if op == "<=":
        return installed <= wanted
    if op == ">":
        return installed > wanted
    if op == "<":
        return installed < wanted
    if op == "~=":
        return installed >= wanted and installed[0] == wanted[0] and installed[1] == wanted[1]
    return None


def check_dependency_drift(spec: str, *, resolver=None) -> Dict[str, Any]:
    """Compare one 'pkg<op>version[,<op>version...]' spec against the installed version.
    `resolver` defaults to importlib.metadata.version; a fixture can inject a fake one."""
    resolver = resolver or importlib_metadata.version
    name, constraints = parse_dependency_spec(spec)
    row: Dict[str, Any] = {"name": name, "spec": spec, "constraints": [f"{op}{v}" for op, v in constraints]}
    try:
        installed = resolver(name)
    except importlib_metadata.PackageNotFoundError:
        row["installed"] = None
        row["satisfied"] = False
        row["drift"] = "not_installed"
        return row
    row["installed"] = installed
    results = [constraint_satisfied(installed, op, target) for op, target in constraints]
    if not constraints:
        row["satisfied"] = True
        row["drift"] = None
    elif any(result is False for result in results):
        row["satisfied"] = False
        row["drift"] = "version_out_of_range"
    elif any(result is None for result in results):
        row["satisfied"] = None
        row["drift"] = "unverifiable"
    else:
        row["satisfied"] = True
        row["drift"] = None
    return row


def build_doctor_report(repo: Path = REPO, *, resolver=None) -> Dict[str, Any]:
    declared_version = None
    dependencies: List[Dict[str, Any]] = []
    errors: List[str] = []
    try:
        declared_version = read_declared_version(repo)
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
    for spec in read_declared_dependencies(repo):
        dependencies.append(check_dependency_drift(spec, resolver=resolver))
    drifted = [row["name"] for row in dependencies if row["satisfied"] is False]
    return {
        "schema": DOCTOR_SCHEMA,
        "package": "simplicio-loop",
        "declared_version": declared_version,
        "dependencies": dependencies,
        "drifted": drifted,
        "errors": errors,
        "clean": not drifted and not errors,
    }


def _print_validation(kind: str, errors: List[str]) -> int:
    if not errors:
        print(f"MEASURED|{kind}: valid")
        return 0
    print(f"UNVERIFIED|{kind}: {len(errors)} error(s)")
    for error in errors:
        print(f"  - {error}")
    return 1


def cmd_validate_component(opts: Dict[str, Any]) -> int:
    path = Path(opts["_positional"][0])
    data = json.loads(path.read_text(encoding="utf-8"))
    return _print_validation(COMPONENT_SCHEMA, validate_component_release(data))


def cmd_validate_ecosystem(opts: Dict[str, Any]) -> int:
    path = Path(opts["_positional"][0])
    data = json.loads(path.read_text(encoding="utf-8"))
    return _print_validation(ECOSYSTEM_SCHEMA, validate_ecosystem_release(data))


def cmd_doctor(opts: Dict[str, Any]) -> int:
    report = build_doctor_report(Path(opts.get("repo", str(REPO))))
    if opts.get("json"):
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        status = "CLEAN" if report["clean"] else "DRIFT"
        print(f"release doctor: {status} (declared {report['declared_version']})")
        for row in report["dependencies"]:
            marker = {True: "ok", False: "DRIFT", None: "unverifiable"}[row["satisfied"]]
            print(f"  - {row['name']} {','.join(row['constraints'])}: "
                  f"installed={row['installed']} [{marker}]")
        for error in report["errors"]:
            print(f"  - error: {error}")
    return 0 if report["clean"] else 1


def cmd_selftest(_opts: Dict[str, Any]) -> int:
    checks: List[Tuple[str, bool]] = []

    def chk(name: str, condition: bool) -> None:
        checks.append((name, condition))

    valid_component = {
        "component": "simplicio-loop", "repo": "wesleysimplicio/simplicio-loop",
        "package": "simplicio-loop", "version": "3.38.0", "commit": "abc123", "tag": "v3.38.0",
        "artifacts": [{"registry": "pypi", "os": "any", "arch": "any",
                        "digest": "sha256:" + "a" * 64, "size": 1024, "signature": "sig"}],
        "compatibility": {"simplicio-cli": ">=0.16.1"},
        "breaking_change": False,
        "changelog": [{"version": "3.38.0", "notes": "release"}],
        "channel": "stable",
    }
    chk("component_release_valid_accepted", validate_component_release(valid_component) == [])

    missing_field = dict(valid_component)
    del missing_field["artifacts"]
    chk("component_release_missing_field_rejected",
        any("missing" in e for e in validate_component_release(missing_field)))

    unknown_field = dict(valid_component)
    unknown_field["totally_unexpected_field"] = True
    chk("component_release_unknown_field_rejected",
        any("unknown" in e for e in validate_component_release(unknown_field)))

    bad_version = dict(valid_component)
    bad_version["version"] = "not-a-version"
    chk("component_release_bad_semver_rejected",
        any("semantic" in e for e in validate_component_release(bad_version)))

    bad_channel = dict(valid_component)
    bad_channel["channel"] = "nightly"
    chk("component_release_bad_channel_rejected",
        any("channel" in e for e in validate_component_release(bad_channel)))

    valid_ecosystem = {
        "release_id": 42,
        "components": {"simplicio-loop": {"version": "3.38.0", "commit": "abc", "digest": "sha256:x"}},
        "graph_hash": "sha256:graph", "contract_hashes": {"loop": "sha256:c"},
        "status": {"loop": "green"}, "evidence": {"e2e": "pass"},
        "rollout": {"canary": "3.38.0"}, "signature": "sig",
    }
    chk("ecosystem_release_valid_accepted", validate_ecosystem_release(valid_ecosystem) == [])

    unknown_eco = dict(valid_ecosystem)
    unknown_eco["mystery"] = 1
    chk("ecosystem_release_unknown_field_rejected",
        any("unknown" in e for e in validate_ecosystem_release(unknown_eco)))

    chk("parse_dependency_spec_basic",
        parse_dependency_spec("simplicio-cli>=0.16.1") == ("simplicio-cli", [(">=", "0.16.1")]))
    chk("constraint_satisfied_ge_true", constraint_satisfied("0.16.1", ">=", "0.16.1") is True)
    chk("constraint_satisfied_ge_false", constraint_satisfied("0.16.0", ">=", "0.16.1") is False)

    def _fake_resolver_ok(_name: str) -> str:
        return "0.16.1"

    row_ok = check_dependency_drift("simplicio-cli>=0.16.1", resolver=_fake_resolver_ok)
    chk("check_dependency_drift_satisfied", row_ok["satisfied"] is True)

    def _fake_resolver_missing(name: str) -> str:
        raise importlib_metadata.PackageNotFoundError(name)

    row_missing = check_dependency_drift("simplicio-cli>=0.16.1", resolver=_fake_resolver_missing)
    chk("check_dependency_drift_not_installed", row_missing["drift"] == "not_installed")

    def _fake_resolver_old(_name: str) -> str:
        return "0.10.0"

    row_old = check_dependency_drift("simplicio-cli>=0.16.1", resolver=_fake_resolver_old)
    chk("check_dependency_drift_out_of_range", row_old["drift"] == "version_out_of_range")

    ok = True
    for name, passed in checks:
        tag = "PASS" if passed else "FAIL"
        print(f"  [{tag}] {name}")
        ok = ok and passed
    n, passed_n = len(checks), sum(1 for _, p in checks if p)
    if ok:
        print(f"MEASURED|component_release selftest: {passed_n}/{n} checks passed")
        return 0
    print(f"UNVERIFIED|component_release selftest: {passed_n}/{n} checks passed (FAILURES ABOVE)")
    return 1


def _parse(args: List[str]) -> Dict[str, Any]:
    opts: Dict[str, Any] = {"_positional": []}
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            key = a[2:]
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                opts[key] = args[i + 1]
                i += 2
            else:
                opts[key] = True
                i += 1
        else:
            opts["_positional"].append(a)
            i += 1
    return opts


def main(argv: Optional[Iterable[str]] = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "--describe-cli":
        print(json.dumps({
            "verbs": ["validate-component", "validate-ecosystem", "doctor", "selftest"],
            "flags": ["--repo", "--json"],
        }))
        return 0
    sub, opts = argv[0], _parse(argv[1:])
    handler = {
        "validate-component": cmd_validate_component,
        "validate-ecosystem": cmd_validate_ecosystem,
        "doctor": cmd_doctor,
        "selftest": cmd_selftest,
    }.get(sub)
    if handler is None:
        print(f"unknown command '{sub}'. choices: validate-component validate-ecosystem doctor selftest")
        return 2
    return handler(opts) or 0


if __name__ == "__main__":
    raise SystemExit(main())
