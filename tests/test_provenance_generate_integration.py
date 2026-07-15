import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.provenance_generate import build_provenance

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, stdin=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, stdin=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, stdin=subprocess.DEVNULL)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, stdin=subprocess.DEVNULL)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, stdin=subprocess.DEVNULL)
    return repo


def test_build_provenance_links_real_artifact_and_source_sha(tmp_path):
    repo = _make_git_repo(tmp_path)
    artifact = tmp_path / "demo.whl"
    artifact.write_bytes(b"fake wheel bytes")

    statement = build_provenance(repo, artifact=artifact)

    assert statement["ok"] is True
    assert statement["ci_attested"] is False
    assert statement["oidc"] is False
    assert statement["builder_identity"] == "local-machine"
    assert statement["subject"][0]["name"] == "demo.whl"
    assert statement["subject"][0]["digest"]["sha256"] == __import__("hashlib").sha256(b"fake wheel bytes").hexdigest()
    assert statement["predicate"]["invocation"]["configSource"]["digest"]["sha1"]
    assert statement["predicate"]["builder"]["id"] == "local-machine"
    assert statement["_type"] == "https://in-toto.io/Statement/v1"


def test_build_provenance_fails_closed_on_missing_artifact(tmp_path):
    repo = _make_git_repo(tmp_path)
    missing_artifact = tmp_path / "does-not-exist.whl"

    statement = build_provenance(repo, artifact=missing_artifact)

    assert statement["ok"] is False
    assert statement["subject"] == []


def test_cli_generate_writes_output_file(tmp_path):
    repo = _make_git_repo(tmp_path)
    artifact = tmp_path / "demo.tar.gz"
    artifact.write_bytes(b"tarball bytes")
    output = tmp_path / "provenance.json"

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "provenance_generate.py"),
         "--repo", str(repo), "generate", "--artifact", str(artifact), "--output", str(output)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )

    assert result.returncode == 0, result.stderr
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["ok"] is True
    assert written["subject"][0]["name"] == "demo.tar.gz"
