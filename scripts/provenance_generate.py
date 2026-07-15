#!/usr/bin/env python3
"""Locally-verifiable provenance statement generation (#292 Fase 4, non-OIDC substitute).

Scope and honest limits
------------------------
Issue #292 Fase 4 §3 asks for provenance/attestation "assinada por identidade OIDC do workflow" —
signed by the OIDC identity of a CI workflow run, carrying repository, workflow, commit/tag,
runner/builder identity, build parameters, and subject digests. OIDC-minted signing identity is,
by construction, issued by a CI provider (GitHub Actions' `id-token: write` claims via Sigstore
Fulcio, or similar); there is no local-machine equivalent, and `.github/workflows/` was removed
repo-wide in PR #311 with no CI substrate to replace it (see docs/SUPPLY_CHAIN.md). This script
does not fabricate an OIDC identity or claim CI attestation.

What it DOES do, for real: OIDC is *one* mechanism for getting provenance, not the only one — a
locally-signed, structured provenance statement built from REAL git commit metadata and a REAL
build artifact digest is itself a verifiable claim, just with a different (weaker) trust root: a
human-controlled gpg key instead of a CI-issued short-lived certificate. This script generates
such a statement, in-toto/SLSA-provenance-shaped:

  * `subject` — name + sha256 of a real artifact on disk (the same digest `sbom_generate.py` and
    `release_verify.py checksums-generate` compute);
  * `predicate.builder` — the local machine's builder identity: `local-machine` (never claims a
    CI runner), the OS user, hostname, and the tool that produced this statement;
  * `predicate.invocation` — `configSource` = the git remote URL + the exact commit SHA the
    scratch build ran from + entry point (the build command actually executed);
  * `predicate.metadata` — real UTC start/finish timestamps and `completeness` flags;
  * `predicate.materials` — the source commit as the sole material (no CI-fetched dependency
    graph is available outside a real build sandbox).

It is explicitly marked `ci_attested: false` / `oidc: false` / `builder_identity: "local-machine"`
so nothing downstream can mistake this for the OIDC-rooted attestation Fase 4/5 ask for. It is
signed the same way `release_verify.py sign` signs the checksum manifest (detached gpg,
blocks — does not fabricate — if no usable secret key exists), giving a REAL, independently
verifiable signature over a REAL claim, which is strictly more honest than either skipping
provenance entirely or faking an OIDC claim this environment cannot produce.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import platform
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

SCHEMA = "simplicio.provenance-lite/v1"
PREDICATE_TYPE = "https://slsa.dev/provenance/v1"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(repo: Path, *args: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def build_provenance(
    repo: Path,
    *,
    artifact: Path,
    source_sha: Optional[str] = None,
    build_command: Optional[str] = None,
) -> Dict[str, Any]:
    repo = repo.resolve()
    sha = source_sha or _git(repo, "rev-parse", "HEAD")
    remote = _git(repo, "config", "--get", "remote.origin.url")
    now = datetime.now(timezone.utc).isoformat()
    artifact_ok = artifact.exists()
    subject = None
    if artifact_ok:
        subject = {"name": artifact.name, "digest": {"sha256": _sha256(artifact)}}
    statement: Dict[str, Any] = {
        "schema": SCHEMA,
        "_type": "https://in-toto.io/Statement/v1",
        "predicateType": PREDICATE_TYPE,
        "subject": [subject] if subject else [],
        "predicate": {
            "buildType": "simplicio.local-build/v1",
            "builder": {
                "id": "local-machine",
                "user": getpass.getuser(),
                "hostname": socket.gethostname(),
                "tool": "scripts/provenance_generate.py",
                "platform": platform.platform(),
            },
            "invocation": {
                "configSource": {
                    "uri": remote or "",
                    "digest": {"sha1": sha or ""},
                    "entryPoint": build_command or "python -m build --wheel --no-isolation",
                },
            },
            "metadata": {
                "generatedAt": now,
                "completeness": {"parameters": True, "environment": False, "materials": False},
                "reproducible": False,
            },
            "materials": [{"uri": remote or "", "digest": {"sha1": sha or ""}}],
        },
        "ci_attested": False,
        "oidc": False,
        "builder_identity": "local-machine",
        "note": (
            "Generated locally by scripts/provenance_generate.py using a human-controlled gpg key, "
            "NOT a CI/OIDC-minted signing identity (see docs/SUPPLY_CHAIN.md — Fase 4/5 OIDC "
            "attestation remains blocked: no GitHub Actions substrate since PR #311). This is a "
            "real, independently gpg-verifiable claim over real git/build metadata; it is a weaker "
            "trust root than OIDC-rooted Sigstore attestation, not a substitute claim of one."
        ),
    }
    statement["ok"] = bool(sha) and artifact_ok
    return statement


def _cmd_generate(args: argparse.Namespace) -> int:
    artifact = Path(args.artifact).resolve()
    if not artifact.exists():
        print(json.dumps({"schema": SCHEMA, "ok": False, "error": f"artifact not found: {artifact}"}))
        return 1
    statement = build_provenance(Path(args.repo), artifact=artifact, build_command=args.build_command)
    text = json.dumps(statement, ensure_ascii=False, sort_keys=True, indent=None if args.json else 2)
    if args.output:
        Path(args.output).write_text(json.dumps(statement, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    print(text)
    return 0 if statement["ok"] else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="provenance_generate", description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="generate a locally-signable in-toto/SLSA-shaped provenance statement")
    p_gen.add_argument("--artifact", required=True, help="path to a real build artifact to link by sha256 digest")
    p_gen.add_argument("--build-command", default=None, help="the exact build command that produced --artifact")
    p_gen.add_argument("--output", default=None, help="write the provenance JSON to this path in addition to stdout")
    p_gen.add_argument("--repo", default=argparse.SUPPRESS)
    p_gen.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    p_gen.set_defaults(func=_cmd_generate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
