import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.release_verify import (
    generate_checksums,
    sign_manifest,
    verify_checksums,
    verify_signature,
)


def _make_artifacts(tmp_path):
    d = tmp_path / "dist"
    d.mkdir()
    (d / "pkg-1.0.0.whl").write_bytes(b"wheel-bytes")
    (d / "pkg-1.0.0.tar.gz").write_bytes(b"sdist-bytes")
    return d


def test_generate_checksums_covers_every_artifact(tmp_path):
    d = _make_artifacts(tmp_path)
    result = generate_checksums(d)
    assert result["ok"] is True
    names = {e["name"] for e in result["artifacts"]}
    assert names == {"pkg-1.0.0.whl", "pkg-1.0.0.tar.gz"}
    for entry in result["artifacts"]:
        assert len(entry["sha256"]) == 64


def test_verify_checksums_round_trip_passes(tmp_path):
    d = _make_artifacts(tmp_path)
    manifest_path = d / "SHA256SUMS.json"
    manifest_path.write_text(json.dumps(generate_checksums(d)), encoding="utf-8")
    result = verify_checksums(d, manifest_path)
    assert result["ok"] is True
    assert result["mismatches"] == []


def test_verify_checksums_detects_tampered_bytes(tmp_path):
    d = _make_artifacts(tmp_path)
    manifest_path = d / "SHA256SUMS.json"
    manifest_path.write_text(json.dumps(generate_checksums(d)), encoding="utf-8")
    # tamper with the artifact AFTER the manifest was captured
    (d / "pkg-1.0.0.whl").write_bytes(b"tampered-bytes")
    result = verify_checksums(d, manifest_path)
    assert result["ok"] is False
    assert any("digest mismatch: pkg-1.0.0.whl" in m for m in result["mismatches"])


def test_verify_checksums_detects_missing_file(tmp_path):
    d = _make_artifacts(tmp_path)
    manifest_path = d / "SHA256SUMS.json"
    manifest_path.write_text(json.dumps(generate_checksums(d)), encoding="utf-8")
    (d / "pkg-1.0.0.tar.gz").unlink()
    result = verify_checksums(d, manifest_path)
    assert result["ok"] is False
    assert any("missing: pkg-1.0.0.tar.gz" in m for m in result["mismatches"])


def test_verify_checksums_detects_undeclared_extra_file(tmp_path):
    d = _make_artifacts(tmp_path)
    manifest_path = d / "SHA256SUMS.json"
    manifest_path.write_text(json.dumps(generate_checksums(d)), encoding="utf-8")
    (d / "sneaky-extra.whl").write_bytes(b"not declared")
    result = verify_checksums(d, manifest_path)
    assert result["ok"] is False
    assert any("undeclared file present: sneaky-extra.whl" in m for m in result["mismatches"])


def test_sign_blocks_when_gpg_missing(tmp_path, monkeypatch):
    manifest_path = tmp_path / "SHA256SUMS.json"
    manifest_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("scripts.release_verify.shutil.which", lambda name: None)
    result = sign_manifest(manifest_path, key_id=None, output=None)
    assert result["ok"] is False
    assert result["blocked"] is True
    assert "gpg not installed" in result["reason"]


def test_sign_blocks_when_no_secret_key_available(tmp_path, monkeypatch):
    manifest_path = tmp_path / "SHA256SUMS.json"
    manifest_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("scripts.release_verify.shutil.which", lambda name: "/usr/bin/gpg")
    monkeypatch.setattr("scripts.release_verify._has_secret_key", lambda key_id: False)
    result = sign_manifest(manifest_path, key_id=None, output=None)
    assert result["ok"] is False
    assert result["blocked"] is True
    assert "no usable gpg secret key" in result["reason"]


def test_verify_signature_blocked_without_gpg(tmp_path, monkeypatch):
    manifest_path = tmp_path / "SHA256SUMS.json"
    manifest_path.write_text("{}", encoding="utf-8")
    sig_path = tmp_path / "SHA256SUMS.json.asc"
    sig_path.write_text("not a real signature", encoding="utf-8")
    monkeypatch.setattr("scripts.release_verify.shutil.which", lambda name: None)
    result = verify_signature(manifest_path, sig_path)
    assert result["ok"] is False
    assert result["blocked"] is True


def test_sign_and_verify_round_trip_when_gpg_key_available(tmp_path):
    if shutil.which("gpg") is None:
        return  # environment has no gpg; the blocked-path tests above cover that branch
    from scripts.release_verify import _has_secret_key
    if not _has_secret_key(None):
        return  # no secret key configured on this machine; nothing to sign with for real
    manifest_path = tmp_path / "SHA256SUMS.json"
    manifest_path.write_text(json.dumps({"a": 1}), encoding="utf-8")
    signed = sign_manifest(manifest_path, key_id=None, output=None)
    assert signed["ok"] is True
    verified = verify_signature(manifest_path, Path(signed["signature"]))
    assert verified["ok"] is True
