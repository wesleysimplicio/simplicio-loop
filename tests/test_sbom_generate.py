import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.sbom_generate import build_sbom

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_repo(tmp_path, deps=('"simplicio-cli>=0.16.1"',)):
    repo = tmp_path / "repo"
    repo.mkdir()
    deps_block = ",\n".join(f"  {d}" for d in deps)
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "demo"\nversion = "1.2.3"\ndependencies = [\n{deps_block}\n]\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, stdin=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True, stdin=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, stdin=subprocess.DEVNULL)
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, stdin=subprocess.DEVNULL)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, stdin=subprocess.DEVNULL)
    return repo


def test_build_sbom_reports_source_sha_and_direct_deps(tmp_path):
    repo = _make_repo(tmp_path, deps=('"simplicio-cli>=0.16.1"',))
    sbom = build_sbom(repo)
    assert sbom["schema"] == "simplicio.sbom/v1"
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["source_sha"]
    assert sbom["ci_attested"] is False
    assert sbom["generated_locally"] is True
    names = [c["name"] for c in sbom["components"]]
    assert names == ["simplicio-cli"]
    assert sbom["ok"] is True


def test_build_sbom_marks_unresolved_component_honestly(tmp_path):
    repo = _make_repo(tmp_path, deps=('"totally-nonexistent-package-xyz"',))
    sbom = build_sbom(repo)
    component = sbom["components"][0]
    assert component["resolved"] is False
    assert "not installed" in component["note"]
    assert "totally-nonexistent-package-xyz" in sbom["unresolved_components"]


def test_build_sbom_links_artifact_digest(tmp_path):
    repo = _make_repo(tmp_path, deps=())
    artifact = tmp_path / "fake.whl"
    artifact.write_bytes(b"hello world")
    sbom = build_sbom(repo, artifact=artifact)
    assert sbom["artifact"]["name"] == "fake.whl"
    assert sbom["artifact"]["sha256"] == \
        "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    assert sbom["artifact"]["size"] == 11


def test_cli_generate_exits_zero_on_repo_root():
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "sbom_generate.py"), "--repo", str(REPO_ROOT),
         "--json", "generate"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["schema"] == "simplicio.sbom/v1"
    assert payload["ci_attested"] is False


def test_cli_generate_fails_on_missing_artifact(tmp_path):
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "sbom_generate.py"), "--repo", str(REPO_ROOT),
         "generate", "--artifact", str(tmp_path / "nope.whl")],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 1
