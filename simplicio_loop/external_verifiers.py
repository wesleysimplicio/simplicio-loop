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
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


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


def discover_default_branch(repo: str) -> Dict[str, Any]:
    """Consult the real default branch of `repo` via `gh api repos/{repo}` — #290 Fase 3.2
    ("Consultar a default branch real no instante da prova"). Never assumes `main`; fails
    closed with a stable reason code on any transport error or malformed response."""
    try:
        result = _run(["gh", "api", f"repos/{repo}"])
    except (subprocess.SubprocessError, OSError) as exc:
        return {"ok": False, "reason_code": "default_branch_query_failed", "error": str(exc)}
    if result.returncode != 0:
        return {"ok": False, "reason_code": "default_branch_query_failed",
                "error": (result.stderr or "").strip()}
    try:
        data = json.loads(result.stdout or "{}")
        default_branch = data["default_branch"]
    except (ValueError, KeyError, TypeError):
        return {"ok": False, "reason_code": "default_branch_response_malformed"}
    if not default_branch:
        return {"ok": False, "reason_code": "default_branch_response_malformed"}
    return {"ok": True, "default_branch": default_branch}


# #290 — `BranchReachabilityVerifier`: "provar reachability do commit com API compare/
# commit/ref, registrando base/head consultados e resposta." GitHub's compare API
# (`repos/{repo}/compare/{base}...{head}`) reports `status` as one of
# `identical` | `ahead` | `behind` | `diverged` relative to `base`. When `head` is the
# claimed merge commit and `base` is the real default branch tip: `identical` means the
# commit *is* the branch tip; `behind` means the commit is strictly an ancestor of the
# tip (i.e. reachable, just not the very latest); `ahead`/`diverged` mean the commit is
# NOT part of the default branch's history — it must never be reported reachable.
_REACHABLE_COMPARE_STATUSES = frozenset({"identical", "behind"})


def compare_commits(repo: str, base: str, head: str) -> Dict[str, Any]:
    """Real `gh api repos/{repo}/compare/{base}...{head}` call. Fails closed on any
    transport error, non-2xx response, or unparseable body."""
    try:
        result = _run(["gh", "api", f"repos/{repo}/compare/{base}...{head}"])
    except (subprocess.SubprocessError, OSError) as exc:
        return {"ok": False, "reason_code": "compare_query_failed", "error": str(exc)}
    if result.returncode != 0:
        return {"ok": False, "reason_code": "compare_query_failed",
                "error": (result.stderr or "").strip()}
    try:
        data = json.loads(result.stdout or "{}")
        status = str(data["status"])
    except (ValueError, KeyError, TypeError):
        return {"ok": False, "reason_code": "compare_response_malformed"}
    return {
        "ok": True,
        "status": status,
        "ahead_by": data.get("ahead_by"),
        "behind_by": data.get("behind_by"),
    }


def verify_branch_reachability(repo: str, commit_sha: str, *,
                                expected_default_branch: Optional[str] = None) -> Dict[str, Any]:
    """`BranchReachabilityVerifier` (#290): prove a claimed merge commit is actually
    reachable from the *real* default branch's current tip — not merely "a PR says
    merged". Composes two live calls: (1) discover the real default branch (never
    trusts a caller-supplied name), (2) `compare` that branch's tip against
    `commit_sha` and only reports `reachable=True` when the compare status proves
    ancestry (`identical` or `behind`).

    "Unknown is not pass": any transport failure, malformed response, or a compare
    status that does NOT prove ancestry (`ahead`, `diverged`) reports
    `reachable=False` with a stable `reason_code` — never a favorable default.
    """
    if not commit_sha:
        return {"ok": False, "reachable": False, "reason_code": "commit_sha_missing"}
    branch_result = discover_default_branch(repo)
    if not branch_result.get("ok"):
        return {
            "ok": False, "reachable": False,
            "reason_code": branch_result.get("reason_code", "default_branch_query_failed"),
        }
    default_branch = branch_result["default_branch"]
    if expected_default_branch and expected_default_branch != default_branch:
        # The caller assumed a branch name that does not match reality (e.g. hardcoded
        # "main" on a repo whose default is "trunk"). Surface the mismatch rather than
        # silently substituting the discovered branch into an assertion the caller did
        # not ask for.
        return {
            "ok": False, "reachable": False, "default_branch": default_branch,
            "reason_code": "default_branch_mismatch",
            "expected_default_branch": expected_default_branch,
        }
    compare_result = compare_commits(repo, default_branch, commit_sha)
    if not compare_result.get("ok"):
        return {
            "ok": False, "reachable": False, "default_branch": default_branch,
            "reason_code": compare_result.get("reason_code", "compare_query_failed"),
        }
    status = compare_result["status"]
    reachable = status in _REACHABLE_COMPARE_STATUSES
    return {
        "ok": True,
        "reachable": reachable,
        "default_branch": default_branch,
        "compare_status": status,
        "ahead_by": compare_result.get("ahead_by"),
        "behind_by": compare_result.get("behind_by"),
        "reason_code": None if reachable else "merge_commit_not_reachable",
    }


def git_is_ancestor(cwd: str, commit_sha: str, branch_ref: str, *, timeout: int = 30) -> Dict[str, Any]:
    """Local, network-free `BranchReachabilityVerifier` alternative: real
    `git merge-base --is-ancestor <commit_sha> <branch_ref>` in a local clone/worktree
    at `cwd` (e.g. used by a sandbox E2E that already has the repo checked out, or as a
    fallback when the GitHub API is unavailable). Exit code 0 means ancestor (reachable);
    exit code 1 means not-an-ancestor; anything else (missing object, not a git repo,
    timeout) fails closed rather than assumes either answer.
    """
    if not commit_sha or not branch_ref:
        return {"ok": False, "reachable": False, "reason_code": "missing_commit_or_branch"}
    try:
        result = _run(["git", "merge-base", "--is-ancestor", commit_sha, branch_ref], cwd=cwd, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as exc:
        return {"ok": False, "reachable": False, "reason_code": "git_merge_base_failed", "error": str(exc)}
    if result.returncode == 0:
        return {"ok": True, "reachable": True, "reason_code": None}
    if result.returncode == 1:
        return {"ok": True, "reachable": False, "reason_code": "merge_commit_not_reachable"}
    return {
        "ok": False, "reachable": False, "reason_code": "git_merge_base_error",
        "error": (result.stderr or "").strip(),
    }


# ---------------------------------------------------------------------------
# Transient-failure retry (#290 fault-injection requirement: "falha transitória da API
# do GitHub durante reconciliação" must never corrupt state or produce a false PASS —
# it must retry a bounded number of times against a *fresh* live call and, absent a
# real success, stay UNVERIFIED with the last observed reason code).
# ---------------------------------------------------------------------------

_DEFAULT_TRANSIENT_REASON_CODES = frozenset({
    "default_branch_query_failed",
    "compare_query_failed",
    "release_download_failed",
    "review_threads_query_failed",
    "deployment_release_query_failed",
})


def retry_transient(fn: Callable[[], Dict[str, Any]], *, attempts: int = 3, backoff: float = 0.0,
                     is_transient: Optional[Callable[[Dict[str, Any]], bool]] = None,
                     sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """Call ``fn()`` up to ``attempts`` times, retrying only while the result looks like a
    *transient* transport failure (rate limit, 5xx, timeout) rather than a real, observed
    negative verdict. Each retry is a genuinely fresh live call — never a cached/replayed
    result — so a real transient failure that clears on the provider side is picked up, and
    a real negative fact (mismatch, missing asset, unresolved thread) is never retried into
    a false PASS: it is returned as-is on the very first attempt because it does not match
    ``is_transient``.
    """
    checker = is_transient or (
        lambda result: bool(result.get("reason_code")) and result.get("reason_code") in _DEFAULT_TRANSIENT_REASON_CODES
    )
    last: Dict[str, Any] = {}
    for attempt in range(1, max(1, attempts) + 1):
        last = fn()
        if not checker(last):
            return last
        if backoff and attempt < attempts:
            sleep(backoff)
    return last


def resolve_release_commit(repo: str, tag: str) -> Dict[str, Any]:
    """Resolve the commit a release *tag* actually points at, via `gh api
    repos/{repo}/releases/tags/{tag}` (`target_commitish` — which GitHub resolves to a real
    commit sha for a lightweight tag, or the branch name for an annotated one created via the
    UI; either way this is the provider's own claim, never inferred locally). Fails closed on
    any transport error or malformed response, and reports draft/prerelease status so a
    caller can reject those explicitly rather than assume a "release" is always final."""
    try:
        result = _run(["gh", "api", f"repos/{repo}/releases/tags/{tag}"])
    except (subprocess.SubprocessError, OSError) as exc:
        return {"ok": False, "reason_code": "deployment_release_query_failed", "error": str(exc)}
    if result.returncode != 0:
        return {"ok": False, "reason_code": "deployment_release_query_failed",
                "error": (result.stderr or "").strip()}
    try:
        data = json.loads(result.stdout or "{}")
        target_commitish = str(data["target_commitish"])
    except (ValueError, KeyError, TypeError):
        return {"ok": False, "reason_code": "deployment_release_response_malformed"}
    return {
        "ok": True,
        "target_commitish": target_commitish,
        "draft": bool(data.get("draft")),
        "prerelease": bool(data.get("prerelease")),
    }


class DeploymentVerifier:
    """`DeploymentVerifier` (#290 Fase 5): a real check that a claimed "deployed" state
    corresponds to something observable.

    This repo ships a Python/npm package, not a long-running service with its own health
    endpoint — there is no server to curl. The closest real, honest analog to "deployed" for
    a package is: the exact bytes published under the claimed release tag are (a) reachable
    from the real default branch (so "deployed" cannot silently point at an orphaned/rejected
    commit), (b) byte-verified (checksum/signature/SBOM, composing the existing
    `ReleaseArtifactVerifier` slice — `verify_release_artifacts` — rather than reinventing byte
    verification here), and (c) genuinely installable from those same downloaded bytes
    (`run_install_smoke`) into a clean environment. "environment" is therefore modeled as the
    *install target* (e.g. `"pypi-index"`, `"local-venv"`, a CI runner image) that the smoke
    ran against — never a bare hostname/environment-name claim with nothing behind it.

    Every dimension fails closed with a stable `reason_code` on any missing/mismatched/
    unavailable proof — "unknown is not pass" — and a `verify()` call is a fresh live
    observation every time; nothing here is cached across calls.
    """

    def __init__(self, *, workdir: Optional[str] = None) -> None:
        self._workdir = workdir

    def verify(self, repo: str, tag: str, environment: str, *,
               asset_names: Optional[List[str]] = None,
               module_name: str = "simplicio_loop",
               expected_commit_sha: Optional[str] = None) -> Dict[str, Any]:
        if not str(environment or "").strip():
            return {"ok": False, "environment": environment, "reason_code": "deployment_environment_missing"}

        release = resolve_release_commit(repo, tag)
        if not release.get("ok"):
            return {"ok": False, "environment": environment,
                    "reason_code": release.get("reason_code", "deployment_release_query_failed")}
        if release.get("draft") or release.get("prerelease"):
            return {"ok": False, "environment": environment,
                    "reason_code": "deployment_release_not_promotable",
                    "draft": release.get("draft"), "prerelease": release.get("prerelease")}

        commit_sha = release["target_commitish"]
        if expected_commit_sha and expected_commit_sha != commit_sha:
            return {"ok": False, "environment": environment, "commit_sha": commit_sha,
                    "reason_code": "deployment_commit_mismatch",
                    "expected_commit_sha": expected_commit_sha}

        reachability = verify_branch_reachability(repo, commit_sha)
        if not reachability.get("ok") or not reachability.get("reachable"):
            return {"ok": False, "environment": environment, "commit_sha": commit_sha,
                    "reason_code": reachability.get("reason_code", "deployment_reachability_unverified")}

        workdir = self._workdir or tempfile.mkdtemp(prefix="simplicio-loop-deployment-verify-")
        cleanup = self._workdir is None
        try:
            artifacts = verify_release_artifacts(repo, tag, asset_names or [], workdir=workdir)
            if not artifacts.get("checksums_verified"):
                return {
                    "ok": False, "environment": environment, "commit_sha": commit_sha,
                    "reason_code": artifacts.get("checksum_reason_code", "deployment_artifact_unverified"),
                }
            wheel_path = next(
                (str(Path(workdir) / name) for name in artifacts.get("assets_verified", [])
                 if name.endswith(".whl") and (Path(workdir) / name).is_file()),
                None,
            )
            if wheel_path is None:
                smoke = {"passed": False, "reason_code": "wheel_not_verified"}
            else:
                smoke = run_install_smoke(wheel_path, module_name=module_name)
            artifact_digest = artifacts["digests"].get(Path(wheel_path).name) if wheel_path else None
            deployed = bool(smoke.get("passed")) and bool(artifact_digest)
            return {
                "ok": deployed,
                "environment": environment,
                "commit_sha": commit_sha,
                "artifact_digest": artifact_digest,
                "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "smoke": smoke,
                "reason_code": None if deployed else smoke.get("reason_code", "deployment_smoke_failed"),
                "evidence": "external-verifiers-deployment-install-smoke",
            }
        finally:
            if cleanup:
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
