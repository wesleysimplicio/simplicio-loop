import subprocess
from pathlib import Path

from simplicio_loop.map_service_git import RepositoryWorktreeRegistry, discover, list_worktrees


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("one\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "initial")
    return root


def test_same_repository_key_contains_main_and_added_worktree(tmp_path):
    root = _repo(tmp_path)
    _git(root, "branch", "feature")
    extra = tmp_path / "feature"
    _git(root, "worktree", "add", "-q", str(extra), "feature")

    record = discover(str(root))
    paths = {Path(worktree.path) for worktree in record.worktrees}
    assert root.resolve() in paths
    assert extra.resolve() in paths
    assert len({record.repository_key}) == 1
    assert {worktree.branch for worktree in record.worktrees} >= {"feature"}


def test_registry_refresh_removes_deleted_worktree(tmp_path):
    root = _repo(tmp_path)
    _git(root, "branch", "feature")
    extra = tmp_path / "feature"
    _git(root, "worktree", "add", "-q", str(extra), "feature")
    registry = RepositoryWorktreeRegistry()
    before = registry.refresh(str(root))
    assert len(before.worktrees) == 2
    _git(root, "worktree", "remove", str(extra))
    after = registry.remove_missing_worktrees(before.repository_key, str(root))
    assert len(after.worktrees) == 1
    assert registry.status()["worktrees"] == 1


def test_unrelated_git_repositories_do_not_collide(tmp_path):
    first = _repo(tmp_path / "a")
    second = _repo(tmp_path / "b")
    assert discover(str(first)).repository_key != discover(str(second)).repository_key
