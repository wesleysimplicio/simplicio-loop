#!/usr/bin/env python3
"""End-to-end LOCAL release-pipeline rehearsal (#292 Fase 6, partial).

Scope and honest limits
------------------------
Issue #292 Fase 6 asks for the same build-once artifact set to be published, byte-identical, to
PyPI, npm, and GitHub Release, then re-verified against each registry. That requires a real
publish target and OIDC-backed CI credentials; neither exists in this environment
(`.github/workflows/` was removed in PR #311, no registry publish has ever happened from this
worktree — see docs/SUPPLY_CHAIN.md). This script does NOT publish anywhere, and does not
simulate a fake "published" result.

What it DOES do, for real: chain every locally-achievable link of the release pipeline — version
bump, build, checksum, sign (best-effort), SBOM, provenance record (best-effort sign), and
clean-room install-smoke — into ONE command against a **disposable copy** of the repository, so a
contributor can prove the whole local chain actually composes end-to-end, not just that each
script works in isolation. Nothing under the real repo checkout is mutated:

  1. `git archive HEAD` the tracked tree into a scratch directory (a real, byte-exact copy of what
     would be tagged — not a hand-picked subset).
  2. Bump the version in that scratch copy only, via `scripts.version_sync.apply_version`
     (defaults to a `+rehearsalNNNNNNNN` local-version-label build tag appended to the current
     canonical version, so it can never collide with a real release version; `--version` overrides
     it for an explicit dry-run of a real bump).
  3. Build a real wheel from the scratch copy (`python -m build --wheel --no-isolation`).
  4. Generate + verify a `SHA256SUMS.json` for the build output (`scripts.release_verify`).
  5. Attempt a detached gpg signature over the checksum manifest — blocks (does not fail the whole
     rehearsal) if no usable secret key is configured on this machine, exactly like
     `release_verify.py sign` does standalone.
  6. Generate a CycloneDX-shaped SBOM linked to the built artifact's digest
     (`scripts.sbom_generate`).
  7. Generate a locally-verifiable provenance statement linked to the same digest + the scratch
     copy's source SHA (`scripts.provenance_generate`), signed the same way as step 5.
  8. Run the clean-room install-smoke (`scripts.install_smoke.run_smoke`) against the scratch
     copy: fresh venv, `--no-deps --no-index`, `PYTHONPATH` cleared, isolation + version asserted,
     `--help` actually executed.

The rehearsal receipt records a Fase-8-shaped state machine —
`planned -> built -> checksummed -> signed|sign_blocked -> sbom -> provenance -> smoke-verified`
— and `ok` is true only if every REQUIRED link succeeded. Signing is optional/best-effort by
design (a fresh machine legitimately has no gpg key yet); it is recorded as `sign_blocked`, not
silently marked `ok`, and downstream consumers can require it via `--require-signing`.

This is a rehearsal, not a release: it never touches the real repo's version files, never creates
a git tag, and never talks to any package registry.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from install_smoke import run_smoke  # noqa: E402
from release_verify import generate_checksums, sign_manifest, verify_checksums  # noqa: E402
from sbom_generate import build_sbom  # noqa: E402
from version_sync import VersionSyncError, apply_version  # noqa: E402
from provenance_generate import build_provenance  # noqa: E402

SCHEMA = "simplicio.release-rehearsal/v1"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=30,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _export_tracked_tree(repo: Path, dest: Path) -> str:
    """Byte-exact copy of the tracked tree at HEAD via `git archive` — never the working tree,
    so uncommitted/ignored cruft in the real checkout can't leak into the rehearsal build."""
    sha = _git(repo, "rev-parse", "HEAD")
    dest.mkdir(parents=True, exist_ok=True)
    archive_path = dest.parent / f"{dest.name}.tar"
    with archive_path.open("wb") as fh:
        result = subprocess.run(
            ["git", "archive", "--format=tar", sha], cwd=repo, stdout=fh,
            stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
        )
    if result.returncode != 0:
        raise RuntimeError(f"git archive failed: {result.stderr.decode(errors='replace')}")
    with tarfile.open(archive_path) as tar:
        tar.extractall(dest)  # noqa: S202 - trusted source (our own repo's HEAD)
    archive_path.unlink(missing_ok=True)
    return sha


def run_rehearsal(
    repo: Path,
    *,
    version: Optional[str] = None,
    require_signing: bool = False,
    keep: bool = False,
) -> Dict[str, Any]:
    repo = repo.resolve()
    workdir = Path(tempfile.mkdtemp(prefix="simplicio-release-rehearsal-"))
    scratch = workdir / "scratch"
    receipt: Dict[str, Any] = {
        "schema": SCHEMA,
        "scope": "local-rehearsal-only (no publish target; see docs/SUPPLY_CHAIN.md)",
        "state": "planned",
        "workdir": str(workdir),
        "steps": {},
    }
    try:
        # Step: export a byte-exact scratch copy of HEAD.
        try:
            source_sha = _export_tracked_tree(repo, scratch)
        except RuntimeError as exc:
            receipt["steps"]["export"] = {"ok": False, "error": str(exc)}
            receipt["ok"] = False
            receipt["reason_code"] = "export_failed"
            return receipt
        receipt["source_sha"] = source_sha
        receipt["steps"]["export"] = {"ok": True, "source_sha": source_sha}

        # Step: bump version in the SCRATCH copy only. Default to a rehearsal-only local-version
        # label (PEP 440 `+rehearsal<epoch>` local segment) so this can never be mistaken for, or
        # collide with, a real published version.
        rehearsal_version = version or _rehearsal_version(scratch)
        try:
            apply_result = apply_version(scratch, rehearsal_version)
        except VersionSyncError as exc:
            receipt["steps"]["version_bump"] = {"ok": False, "error": str(exc)}
            receipt["ok"] = False
            receipt["reason_code"] = "version_bump_failed"
            return receipt
        receipt["rehearsal_version"] = rehearsal_version
        receipt["steps"]["version_bump"] = {
            "ok": apply_result["ok"],
            "changed_files": apply_result["changed_files"],
        }
        if not apply_result["ok"]:
            receipt["ok"] = False
            receipt["reason_code"] = "version_bump_not_ready"
            return receipt
        receipt["state"] = "built"

        # Step: build a real wheel from the scratch copy.
        dist_dir = workdir / "dist"
        dist_dir.mkdir(parents=True, exist_ok=True)
        build_cmd = [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(dist_dir)]
        build = subprocess.run(build_cmd, cwd=scratch, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        wheels = sorted(dist_dir.glob("*.whl"))
        receipt["steps"]["build"] = {
            "command": " ".join(build_cmd),
            "returncode": build.returncode,
            "stderr_tail": build.stderr.strip().splitlines()[-10:] if build.stderr else [],
            "wheel": str(wheels[-1]) if wheels else None,
            "ok": build.returncode == 0 and bool(wheels),
        }
        if not receipt["steps"]["build"]["ok"]:
            receipt["ok"] = False
            receipt["reason_code"] = "build_failed"
            return receipt
        wheel_path = wheels[-1]
        receipt["state"] = "checksummed"

        # Step: checksums generate + verify.
        checksums = generate_checksums(dist_dir)
        manifest_path = dist_dir / "SHA256SUMS.json"
        manifest_path.write_text(json.dumps(checksums, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        verify = verify_checksums(dist_dir, manifest_path)
        receipt["steps"]["checksums"] = {"generate": checksums, "verify": verify, "ok": checksums["ok"] and verify["ok"]}
        if not receipt["steps"]["checksums"]["ok"]:
            receipt["ok"] = False
            receipt["reason_code"] = "checksums_failed"
            return receipt

        # Step: best-effort gpg signature over the checksum manifest.
        sign_result = sign_manifest(manifest_path, key_id=None, output=None)
        signing_ok = sign_result["ok"]
        receipt["steps"]["sign"] = sign_result
        receipt["state"] = "signed" if signing_ok else "sign_blocked"
        if require_signing and not signing_ok:
            receipt["ok"] = False
            receipt["reason_code"] = "signing_required_but_blocked"
            return receipt

        # Step: SBOM linked to the built artifact digest. `scratch` is a `git archive` export with
        # no `.git` directory, so pass the source SHA explicitly rather than let `build_sbom`'s
        # own `git rev-parse` fail against a repo-less directory.
        sbom = build_sbom(scratch, artifact=wheel_path, source_sha=source_sha)
        sbom_path = dist_dir / "sbom.json"
        sbom_path.write_text(json.dumps(sbom, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        receipt["steps"]["sbom"] = {"ok": sbom["ok"], "path": str(sbom_path)}
        if not sbom["ok"]:
            receipt["ok"] = False
            receipt["reason_code"] = "sbom_failed"
            return receipt
        receipt["state"] = "sbom"

        # Step: locally-verifiable provenance statement, signed the same best-effort way.
        provenance = build_provenance(scratch, artifact=wheel_path, source_sha=source_sha)
        provenance_path = dist_dir / "provenance.json"
        provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        prov_sign = sign_manifest(provenance_path, key_id=None, output=None) if signing_ok else {
            "ok": False, "blocked": True, "reason": "checksum manifest signing already blocked (no gpg key)"
        }
        receipt["steps"]["provenance"] = {"ok": provenance["ok"], "path": str(provenance_path), "sign": prov_sign}
        if not provenance["ok"]:
            receipt["ok"] = False
            receipt["reason_code"] = "provenance_failed"
            return receipt
        receipt["state"] = "provenance"

        # Step: clean-room install-smoke against the scratch copy (rebuilds internally by design
        # — install_smoke.py is the standalone, independently-runnable clean-room proof; reusing
        # its own build step keeps this rehearsal from silently depending on step ordering above).
        smoke = run_smoke(scratch, expected_version=rehearsal_version, keep=False)
        receipt["steps"]["install_smoke"] = smoke
        if not smoke.get("ok"):
            receipt["ok"] = False
            receipt["reason_code"] = "install_smoke_failed"
            return receipt
        receipt["state"] = "smoke-verified"

        receipt["ok"] = True
        return receipt
    finally:
        if keep:
            receipt["kept_workdir"] = True
        else:
            shutil.rmtree(workdir, ignore_errors=True)
            receipt.pop("workdir", None)


def _rehearsal_version(scratch: Path) -> str:
    from release_manifest import build_manifest as _build_manifest
    canonical = _build_manifest(scratch)["canonical_version"]
    return f"{canonical}+rehearsal{int(time.time())}"


def _cmd_run(args: argparse.Namespace) -> int:
    result = run_rehearsal(
        Path(args.repo),
        version=args.version,
        require_signing=args.require_signing,
        keep=args.keep,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=None if args.json else 2))
    if args.output:
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    return 0 if result.get("ok") else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="release_rehearsal", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="chain version-bump+build+checksum+sign+sbom+provenance+smoke against a disposable scratch copy")
    p_run.add_argument("--repo", default=".")
    p_run.add_argument("--version", default=None, help="explicit version to rehearse a real bump (default: safe +rehearsal<ts> label)")
    p_run.add_argument("--require-signing", action="store_true", help="fail the rehearsal if no gpg key is available (default: sign is best-effort)")
    p_run.add_argument("--keep", action="store_true", help="keep the scratch workdir for inspection")
    p_run.add_argument("--output", default=None, help="also write the receipt JSON to this path")
    p_run.add_argument("--json", action="store_true", help="emit compact single-line JSON")
    p_run.set_defaults(func=_cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
