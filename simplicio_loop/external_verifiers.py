"""Byte-level release-artifact verifiers (#290 Fase 4).

`simplicio_loop/source_state.py` used to write `checksums_verified`/`signatures_verified`/
`sbom_present`/`install_smoke.passed` as hardcoded `True` on any tag lookup, without ever
downloading or inspecting a single byte. This module is the first real
`ReleaseArtifactVerifier`/`AttestationVerifier`/`SbomVerifier`/`InstallSmokeVerifier` slice named
in #290's proposed architecture: it downloads the *actual* release asset bytes via `gh release
download`, recomputes their SHA-256 digest, compares against a published checksum manifest (if
one exists), attempts `gh attestation verify` (GitHub's Sigstore-backed build-provenance check)
against the downloaded bytes, looks for and parses an SBOM asset, and — for install smoke —
actually creates a throwaway venv, `pip install`s the downloaded wheel into it, and imports the
package for real.

"Unknown is not pass": every one of these functions returns a stable `reason_code` instead of a
favorable default whenever the underlying proof is missing, incomplete, or the tooling itself is
unavailable. A verified `True` is only ever set on the branch where the actual check ran and
passed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional


def _run(args, cwd=None, timeout=180):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd)


def sha256_file(path) -> str:
    """Real SHA-256 digest of a file's bytes on disk — not a name/size heuristic."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


_CHECKSUM_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+)$")
_CHECKSUM_LINE_RE_ALT = re.compile(r"^(.+?):\s*([0-9a-fA-F]{64})$")


def parse_checksum_manifest(text: str) -> Dict[str, str]:
    """Parse a `sha256sum`-style or `name: hash`-style checksum manifest into
    `{filename: lowercase_hex_digest}`. Unparseable lines are skipped, never guessed."""
    out: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _CHECKSUM_LINE_RE.match(line)
        if match:
            out[match.group(2).strip()] = match.group(1).lower()
            continue
        match = _CHECKSUM_LINE_RE_ALT.match(line)
        if match:
            out[match.group(1).strip()] = match.group(2).lower()
    return out


_CHECKSUM_MANIFEST_NAMES = {"sha256sums", "sha256sums.txt", "checksums.txt", "checksums.sha256"}
_SBOM_NAME_RE = re.compile(r"sbom|\.spdx\.json$|\.cdx\.json$|cyclonedx", re.IGNORECASE)
_SIGNATURE_SUFFIXES = (".sig", ".asc", ".intoto.jsonl", ".att")


def download_release_assets(repo: str, tag: str, dest_dir, *, asset_names: Optional[List[str]] = None) -> Dict[str, Any]:
    """Download the real release asset bytes via `gh release download`. Fails closed
    (`ok=False`, `reason_code`) rather than raise on a subprocess or CLI error."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    args = ["release", "download", tag, "--repo", repo, "--dir", str(dest), "--clobber"]
    if asset_names:
        for name in asset_names:
            args += ["--pattern", name]
    try:
        result = _run(["gh"] + args, timeout=180)
    except (subprocess.SubprocessError, OSError) as exc:
        return {"ok": False, "reason_code": "release_download_failed", "error": str(exc), "downloaded": []}
    if result.returncode != 0:
        return {"ok": False, "reason_code": "release_download_failed",
                "error": (result.stderr or "").strip(), "downloaded": []}
    downloaded = sorted(str(path) for path in dest.iterdir() if path.is_file())
    return {"ok": True, "downloaded": downloaded}


def verify_release_artifacts(repo: str, tag: str, asset_names: List[str], *,
                              workdir: Optional[str] = None) -> Dict[str, Any]:
    """Real byte-level verification of a GitHub release's assets.

    Returns a dict with `checksums_verified`, `signatures_verified`, `sbom_present` (each
    `False` unless the corresponding check actually ran and passed), a `reason_code` per
    dimension when unverified, the recomputed `digests` for every downloaded file, and the
    list of `assets_verified`.
    """
    cleanup = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="simplicio-loop-release-verify-")
    try:
        download = download_release_assets(repo, tag, workdir, asset_names=asset_names)
        result: Dict[str, Any] = {
            "checksums_verified": False, "checksum_reason_code": None,
            "signatures_verified": False, "signature_reason_code": None,
            "sbom_present": False, "sbom_reason_code": None,
            "digests": {}, "assets_verified": [],
        }
        if not download["ok"]:
            result["checksum_reason_code"] = download["reason_code"]
            result["signature_reason_code"] = download["reason_code"]
            result["sbom_reason_code"] = download["reason_code"]
            return result

        local_files = {Path(path).name: Path(path) for path in download["downloaded"]}
        digests = {name: sha256_file(path) for name, path in local_files.items()}
        result["digests"] = digests

        manifest_name = next((name for name in local_files if name.lower() in _CHECKSUM_MANIFEST_NAMES), None)
        signature_assets = [name for name in local_files if name.lower().endswith(_SIGNATURE_SUFFIXES)]
        sbom_name = next((name for name in local_files if _SBOM_NAME_RE.search(name.lower())), None)
        artifact_names = [
            name for name in local_files
            if name != manifest_name and name not in signature_assets and name != sbom_name
        ]

        if manifest_name is None:
            result["checksum_reason_code"] = "checksum_manifest_absent"
        else:
            manifest_digests = parse_checksum_manifest(local_files[manifest_name].read_text(errors="replace"))
            if not manifest_digests:
                result["checksum_reason_code"] = "checksum_manifest_unparseable"
            elif not artifact_names:
                result["checksum_reason_code"] = "no_artifact_assets"
            else:
                missing = [name for name in artifact_names if name not in manifest_digests]
                mismatches = [
                    name for name in artifact_names
                    if name in manifest_digests and manifest_digests[name] != digests[name]
                ]
                if missing:
                    result["checksum_reason_code"] = "checksum_manifest_missing_entry"
                elif mismatches:
                    result["checksum_reason_code"] = "checksum_mismatch"
                else:
                    result["checksums_verified"] = True
                    result["assets_verified"] = sorted(artifact_names)

        if not artifact_names:
            result["signature_reason_code"] = "no_artifact_assets"
        else:
            any_checked = False
            all_verified = True
            for name in artifact_names:
                path = local_files[name]
                try:
                    attestation = _run(["gh", "attestation", "verify", str(path), "--repo", repo])
                except (subprocess.SubprocessError, OSError):
                    all_verified = False
                    continue
                any_checked = True
                if attestation.returncode != 0:
                    all_verified = False
            if not any_checked:
                result["signature_reason_code"] = "attestation_check_unavailable"
            elif all_verified:
                result["signatures_verified"] = True
            else:
                result["signature_reason_code"] = "attestation_not_found"

        if sbom_name is None:
            result["sbom_reason_code"] = "sbom_asset_absent"
        else:
            try:
                sbom_doc = json.loads(local_files[sbom_name].read_text(errors="replace"))
            except (OSError, ValueError):
                result["sbom_reason_code"] = "sbom_unparseable"
            else:
                is_spdx = isinstance(sbom_doc, dict) and bool(sbom_doc.get("spdxVersion"))
                is_cdx = (
                    isinstance(sbom_doc, dict)
                    and str(sbom_doc.get("bomFormat", "")).lower() == "cyclonedx"
                )
                if is_spdx or is_cdx:
                    result["sbom_present"] = True
                else:
                    result["sbom_reason_code"] = "sbom_format_unrecognized"
        return result
    finally:
        if cleanup:
            shutil.rmtree(workdir, ignore_errors=True)


def verify_release(repo: str, tag: str, asset_names: List[str], *,
                    module_name: str = "simplicio_loop") -> Dict[str, Any]:
    """Orchestrates the full release verification for one tag: downloads the assets once,
    runs `verify_release_artifacts` over the real bytes, then — only when a wheel asset was
    downloaded and its checksum verified — runs a real install smoke against that same
    downloaded wheel (never the local checkout). Returns the merged dict plus
    `install_smoke` (`passed`/`reason_code`)."""
    workdir = tempfile.mkdtemp(prefix="simplicio-loop-release-verify-")
    try:
        result = verify_release_artifacts(repo, tag, asset_names, workdir=workdir)
        wheel_path = next(
            (str(Path(workdir) / name) for name in result.get("assets_verified", [])
             if name.endswith(".whl") and (Path(workdir) / name).is_file()),
            None,
        )
        if wheel_path is None:
            result["install_smoke"] = {"passed": False, "reason_code": "wheel_not_verified"}
        else:
            result["install_smoke"] = run_install_smoke(wheel_path, module_name=module_name)
        return result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_install_smoke(wheel_path: str, *, module_name: str = "simplicio_loop") -> Dict[str, Any]:
    """Real install smoke: create a throwaway venv, `pip install` the given wheel bytes into
    it (no reuse of the local checkout or any ambient site-packages), and import the package
    for real. Returns `passed=True` only when venv creation, install, and import all
    succeeded; otherwise `passed=False` with a stable `reason_code`."""
    if not wheel_path or not os.path.isfile(wheel_path):
        return {"passed": False, "reason_code": "wheel_not_found"}
    venv_dir = tempfile.mkdtemp(prefix="simplicio-loop-install-smoke-")
    try:
        created = _run([sys.executable, "-m", "venv", venv_dir], timeout=120)
        if created.returncode != 0:
            return {"passed": False, "reason_code": "venv_create_failed", "log": created.stderr[-2000:]}
        bin_dir = "Scripts" if os.name == "nt" else "bin"
        python_name = "python.exe" if os.name == "nt" else "python"
        python_bin = str(Path(venv_dir) / bin_dir / python_name)
        installed = _run(
            [python_bin, "-m", "pip", "install", "--no-index",
             "--find-links", str(Path(wheel_path).parent), wheel_path],
            timeout=180,
        )
        if installed.returncode != 0:
            return {"passed": False, "reason_code": "install_failed",
                     "log": (installed.stdout[-2000:] + installed.stderr[-2000:])}
        probe = _run([python_bin, "-c", f"import {module_name}; print({module_name}.__version__)"], timeout=60)
        if probe.returncode != 0:
            return {"passed": False, "reason_code": "import_smoke_failed",
                     "log": (probe.stdout + probe.stderr)[-2000:]}
        return {"passed": True, "reason_code": None, "version": probe.stdout.strip()}
    except (subprocess.SubprocessError, OSError) as exc:
        return {"passed": False, "reason_code": "install_smoke_error", "error": str(exc)}
    finally:
        shutil.rmtree(venv_dir, ignore_errors=True)
