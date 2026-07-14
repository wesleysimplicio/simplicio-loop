#!/usr/bin/env python3
"""Local checksum + signature verification for build artifacts (#292 Fase 4/6, partial).

Scope and honest limits
------------------------
Issue #292 asks for `SHA256SUMS` + a keyless/Sigstore or OIDC-native attestation signature,
verified by an independent CI job before publication, and for publishers to compare digests
against every registry (PyPI/npm/GitHub Release). None of that is achievable right now:
`.github/workflows/` was removed in PR #311 (GitHub Actions billing lockout) and this repo does
not publish to any registry from this environment — there is no CI substrate to hold an OIDC
token, and no keyless/Sigstore tooling (`cosign`/`sigstore`) is installed here.

What this script DOES do, for real, locally:

  * `checksums generate` — walks a directory of build artifacts (e.g. `dist/`) and writes a
    deterministic `SHA256SUMS.json` (name, size, sha256) — the same digest data Fase 4 §1 wants,
    just produced locally instead of inside a build-once CI job.
  * `checksums verify` — recomputes digests and fails closed on any mismatch, missing file, or
    extra undeclared file.
  * `sign` — detached-signs the checksum manifest with `gpg` IF a usable secret key is available
    on this machine. If `gpg` is missing or no secret key is configured, it exits non-zero with an
    explicit `blocked` reason — it never fabricates a signature or claims one exists.
  * `verify-signature` — runs `gpg --verify` against a real detached signature.

This intentionally does not claim OIDC/Sigstore/Trusted-Publishing coverage (Fase 5) — gpg
detached-signing is a strictly weaker, machine-local substitute, documented as such in
docs/SUPPLY_CHAIN.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = "simplicio.release-verify/v1"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def generate_checksums(directory: Path) -> Dict[str, Any]:
    directory = directory.resolve()
    entries: List[Dict[str, Any]] = []
    if directory.is_dir():
        for path in sorted(directory.iterdir()):
            if path.is_file() and not path.name.endswith((".json", ".asc", ".sig")):
                entries.append({"name": path.name, "size": path.stat().st_size, "sha256": _sha256(path)})
    return {
        "schema": SCHEMA,
        "action": "checksums-generate",
        "directory": str(directory),
        "artifacts": entries,
        "ok": bool(entries),
    }


def verify_checksums(directory: Path, manifest_path: Path) -> Dict[str, Any]:
    directory = directory.resolve()
    mismatches: List[str] = []
    if not manifest_path.exists():
        return {"schema": SCHEMA, "action": "checksums-verify", "ok": False,
                "error": f"manifest not found: {manifest_path}"}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    declared = {entry["name"]: entry for entry in manifest.get("artifacts", [])}
    actual_files = {
        p.name for p in directory.iterdir()
        if directory.is_dir() and p.is_file() and not p.name.endswith((".json", ".asc", ".sig"))
    } if directory.is_dir() else set()
    for name, entry in declared.items():
        path = directory / name
        if not path.exists():
            mismatches.append(f"missing: {name}")
            continue
        actual = _sha256(path)
        if actual != entry.get("sha256"):
            mismatches.append(f"digest mismatch: {name} (expected {entry.get('sha256')}, got {actual})")
        if path.stat().st_size != entry.get("size"):
            mismatches.append(f"size mismatch: {name}")
    extra = sorted(actual_files - set(declared))
    for name in extra:
        mismatches.append(f"undeclared file present: {name}")
    return {
        "schema": SCHEMA,
        "action": "checksums-verify",
        "directory": str(directory),
        "manifest": str(manifest_path),
        "mismatches": mismatches,
        "ok": not mismatches,
    }


def _gpg_available() -> Optional[str]:
    return shutil.which("gpg")


def _has_secret_key(key_id: Optional[str]) -> bool:
    gpg = _gpg_available()
    if not gpg:
        return False
    cmd = [gpg, "--list-secret-keys", "--with-colons"]
    if key_id:
        cmd.append(key_id)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError:
        # e.g. a broken/inherited std handle in some sandboxed process environments; treat as
        # "cannot confirm a usable key" rather than crash — fail closed, never fabricate.
        return False
    if result.returncode != 0:
        return False
    return "sec:" in result.stdout


def sign_manifest(manifest_path: Path, *, key_id: Optional[str], output: Optional[Path]) -> Dict[str, Any]:
    gpg = _gpg_available()
    if not gpg:
        return {"schema": SCHEMA, "action": "sign", "ok": False, "blocked": True,
                "reason": "gpg not installed on this machine; cannot produce a real signature "
                          "(refusing to fabricate one)"}
    if not manifest_path.exists():
        return {"schema": SCHEMA, "action": "sign", "ok": False,
                "error": f"manifest not found: {manifest_path}"}
    if not _has_secret_key(key_id):
        return {"schema": SCHEMA, "action": "sign", "ok": False, "blocked": True,
                "reason": ("no usable gpg secret key available" +
                           (f" for key-id {key_id}" if key_id else "") +
                           "; cannot produce a real signature (refusing to fabricate one). "
                           "Configure a signing key and re-run, or accept this as a documented "
                           "Fase 4/5 gap until Sigstore/OIDC keyless signing is available.")}
    sig_path = output or manifest_path.with_suffix(manifest_path.suffix + ".asc")
    if sig_path.exists():
        sig_path.unlink()
    cmd = [gpg, "--batch", "--yes", "--armor", "--detach-sign", "--output", str(sig_path)]
    if key_id:
        cmd += ["--local-user", key_id]
    cmd.append(str(manifest_path))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError as exc:
        return {"schema": SCHEMA, "action": "sign", "ok": False, "error": f"gpg sign failed: {exc}"}
    if result.returncode != 0 or not sig_path.exists():
        return {"schema": SCHEMA, "action": "sign", "ok": False,
                "error": f"gpg sign failed rc={result.returncode}: {result.stderr.strip()}"}
    return {"schema": SCHEMA, "action": "sign", "ok": True,
            "manifest": str(manifest_path), "signature": str(sig_path)}


def verify_signature(manifest_path: Path, sig_path: Path) -> Dict[str, Any]:
    gpg = _gpg_available()
    if not gpg:
        return {"schema": SCHEMA, "action": "verify-signature", "ok": False, "blocked": True,
                "reason": "gpg not installed on this machine; cannot verify a real signature"}
    if not manifest_path.exists() or not sig_path.exists():
        return {"schema": SCHEMA, "action": "verify-signature", "ok": False,
                "error": "manifest or signature file not found"}
    try:
        result = subprocess.run([gpg, "--verify", str(sig_path), str(manifest_path)],
                                 capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError as exc:
        return {"schema": SCHEMA, "action": "verify-signature", "ok": False,
                "error": f"gpg verify failed: {exc}"}
    return {"schema": SCHEMA, "action": "verify-signature", "ok": result.returncode == 0,
            "detail": result.stderr.strip()}


def _cmd_checksums_generate(args: argparse.Namespace) -> int:
    result = generate_checksums(Path(args.dir))
    if args.output:
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


def _cmd_checksums_verify(args: argparse.Namespace) -> int:
    result = verify_checksums(Path(args.dir), Path(args.manifest))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


def _cmd_sign(args: argparse.Namespace) -> int:
    result = sign_manifest(Path(args.file), key_id=args.key_id, output=Path(args.output) if args.output else None)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else (3 if result.get("blocked") else 1)


def _cmd_verify_signature(args: argparse.Namespace) -> int:
    result = verify_signature(Path(args.file), Path(args.sig))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else (3 if result.get("blocked") else 1)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="release_verify", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("checksums-generate", help="write a SHA256SUMS.json for a directory of artifacts")
    p_gen.add_argument("--dir", required=True)
    p_gen.add_argument("--output", default=None)
    p_gen.set_defaults(func=_cmd_checksums_generate)

    p_ver = sub.add_parser("checksums-verify", help="verify artifacts in a directory against a manifest")
    p_ver.add_argument("--dir", required=True)
    p_ver.add_argument("--manifest", required=True)
    p_ver.set_defaults(func=_cmd_checksums_verify)

    p_sign = sub.add_parser("sign", help="gpg-detach-sign a manifest (blocks if no key available)")
    p_sign.add_argument("--file", required=True)
    p_sign.add_argument("--key-id", default=None)
    p_sign.add_argument("--output", default=None)
    p_sign.set_defaults(func=_cmd_sign)

    p_vs = sub.add_parser("verify-signature", help="gpg --verify a detached signature")
    p_vs.add_argument("--file", required=True)
    p_vs.add_argument("--sig", required=True)
    p_vs.set_defaults(func=_cmd_verify_signature)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
