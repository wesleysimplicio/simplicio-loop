from __future__ import annotations

import subprocess

import pytest

from simplicio_loop.github_drain_intake import DrainIntakeError, ReadOnlyLocalGitMap


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def test_real_git_canonical_map_is_read_only_and_remote_bound(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "app.py").write_text("print('ok')\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "remote", "add", "origin", "git@github.com:acme/widgets.git")
    before = _git(repo, "status", "--porcelain")

    mapping = ReadOnlyLocalGitMap()
    receipt = mapping.prepare_canonical("acme/widgets", str(repo))

    assert receipt["status"] == "ready"
    assert receipt["mode"] == "canonical"
    assert receipt["files"] == 1
    assert receipt["cache_key"]
    assert _git(repo, "status", "--porcelain") == before
    assert mapping._repo_from_remote("https://github.com/acme/widgets.git") == "acme/widgets"

    with pytest.raises(DrainIntakeError) as mismatch:
        mapping.prepare_canonical("acme/other", str(repo))
    assert mismatch.value.reason_code == "workspace_repository_mismatch"
