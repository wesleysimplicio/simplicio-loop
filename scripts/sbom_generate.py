#!/usr/bin/env python3
"""Local, CLI-driven SBOM generation for simplicio-loop (#292 Fase 4, partial).

Scope and honest limits
------------------------
Issue #292 Fase 4 asks for an SBOM "ligado aos digests publicados" produced inside a build-once
CI pipeline with OIDC-signed provenance. `.github/workflows/` was removed repo-wide in PR #311
after a GitHub Actions billing lockout, and there is currently no CI substrate to run such a
pipeline on (see docs/SUPPLY_CHAIN.md). This script does NOT fabricate a CI-produced SBOM. It
generates a real, deterministic CycloneDX-shaped SBOM **locally**, from:

  * `pyproject.toml` — direct declared dependencies (name + version constraint);
  * the local Python environment via `importlib.metadata` — resolved version + license for every
    dependency that is actually installed (best-effort: this is NOT a full transitive dependency
    resolver; it reports what `importlib.metadata` can see, and says so explicitly per component);
  * the current `git rev-parse HEAD` — the source SHA the SBOM was generated against;
  * an optional `--artifact PATH` — sha256 digest of a real build artifact (wheel/sdist/tgz) so
    the SBOM is linked to actual bytes on disk, not just a tag.

This is real, runnable, and testable today. It is not a substitute for Fase 4's CI-attested,
OIDC-signed SBOM — that remains blocked until a CI substrate exists again.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from importlib import metadata as importlib_metadata
except ImportError:  # pragma: no cover - py<3.8 not supported by this repo
    import importlib_metadata  # type: ignore

SCHEMA = "simplicio.sbom/v1"
CYCLONEDX_SPEC_VERSION = "1.5"

DEP_RE = re.compile(r'^\s*"([A-Za-z0-9_.\-]+)\s*([<>=!~].*)?"\s*,?\s*$')


def _repo_root(repo: Path) -> Path:
    return repo.resolve()


def _git_sha(repo: Path) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _parse_pyproject_dependencies(pyproject: Path) -> List[str]:
    """Extract `[project.dependencies]` array entries without a TOML parser dependency."""
    text = pyproject.read_text(encoding="utf-8")
    match = re.search(r"(?ms)^dependencies\s*=\s*\[(.*?)\]", text)
    if not match:
        return []
    names: List[str] = []
    for line in match.group(1).splitlines():
        m = DEP_RE.match(line)
        if m:
            names.append(m.group(1))
    return names


def _component_for(name: str) -> Dict[str, Any]:
    component: Dict[str, Any] = {
        "type": "library",
        "name": name,
        "version": "",
        "licenses": [],
        "resolved": False,
    }
    try:
        dist = importlib_metadata.distribution(name)
    except importlib_metadata.PackageNotFoundError:
        component["note"] = "not installed in this environment; version/license unresolved"
        return component
    component["version"] = dist.version
    component["resolved"] = True
    license_expr = dist.metadata.get("License") if dist.metadata else None
    if license_expr and license_expr.upper() != "UNKNOWN":
        component["licenses"] = [{"license": {"name": license_expr}}]
    else:
        for classifier in dist.metadata.get_all("Classifier") or []:
            if classifier.startswith("License ::"):
                component["licenses"].append({"license": {"name": classifier.split("::")[-1].strip()}})
    return component


def _artifact_digest(path: Path) -> Dict[str, Any]:
    data = path.read_bytes()
    return {
        "name": path.name,
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def build_sbom(repo: Path, *, artifact: Optional[Path] = None) -> Dict[str, Any]:
    repo = _repo_root(repo)
    pyproject = repo / "pyproject.toml"
    direct_deps = _parse_pyproject_dependencies(pyproject) if pyproject.exists() else []
    components = [_component_for(name) for name in direct_deps]
    unresolved = [c["name"] for c in components if not c["resolved"]]
    sbom: Dict[str, Any] = {
        "schema": SCHEMA,
        "bomFormat": "CycloneDX",
        "specVersion": CYCLONEDX_SPEC_VERSION,
        "source_sha": _git_sha(repo),
        "generated_locally": True,
        "ci_attested": False,
        "note": (
            "Generated locally by scripts/sbom_generate.py, not by a CI/OIDC-attested build "
            "(see docs/SUPPLY_CHAIN.md — Fase 4 CI attestation is blocked, no GitHub Actions "
            "substrate since PR #311)."
        ),
        "components": components,
        "unresolved_components": unresolved,
        "artifact": _artifact_digest(artifact) if artifact else None,
    }
    sbom["ok"] = bool(sbom["source_sha"]) and (artifact is None or sbom["artifact"] is not None)
    return sbom


def _cmd_generate(args: argparse.Namespace) -> int:
    artifact = Path(args.artifact).resolve() if args.artifact else None
    if artifact and not artifact.exists():
        print(json.dumps({"schema": SCHEMA, "ok": False, "error": f"artifact not found: {artifact}"}))
        return 1
    sbom = build_sbom(Path(args.repo), artifact=artifact)
    text = json.dumps(sbom, ensure_ascii=False, sort_keys=True, indent=2 if not args.json else None)
    if args.output:
        Path(args.output).write_text(text if args.json else json.dumps(sbom, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    print(text)
    return 0 if sbom["ok"] else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sbom_generate", description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--json", action="store_true", help="emit compact single-line JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="generate a local CycloneDX-shaped SBOM")
    p_gen.add_argument("--artifact", default=None, help="path to a real build artifact to link by sha256 digest")
    p_gen.add_argument("--output", default=None, help="write the SBOM JSON to this path in addition to stdout")
    p_gen.add_argument("--repo", default=argparse.SUPPRESS)
    p_gen.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    p_gen.set_defaults(func=_cmd_generate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
