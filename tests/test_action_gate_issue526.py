from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

HOOK = Path(__file__).parents[1] / "hooks" / "action_gate.py"


def load_hook():
    spec = importlib.util.spec_from_file_location("action_gate_issue526", HOOK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._runtime_gate_escalation = lambda command: None
    return module


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def make_repo(tmp_path: Path, text: str = "ok") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "file.txt").write_text(text, encoding="utf-8")
    git(repo, "add", "file.txt")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def test_push_from_worktree_scans_push_range(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "file.txt").write_text("AKIAQRSTUVWX01234567", encoding="utf-8")
    git(repo, "add", "file.txt")
    git(repo, "commit", "-q", "-m", "secret")
    hook = load_hook()
    verdict = hook.gate_command('cd "%s" && git push origin feature' % repo)
    assert verdict["action"] == "block"
    assert "push diff" in verdict["reason"]
    assert str(repo) in verdict["reason"]


def test_clean_push_is_deterministic_five_times(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "safe")
    (repo / "file.txt").write_text("safe-2", encoding="utf-8")
    git(repo, "add", "file.txt")
    git(repo, "commit", "-q", "-m", "safe")
    hook = load_hook()
    verdicts = [
        hook.gate_command('cd "%s" && git push origin feature' % repo)
        for _ in range(5)
    ]
    assert verdicts == [{"action": "allow", "reason": ""}] * 5


def test_git_c_resolves_effective_repo_for_push(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "file.txt").write_text("AKIAQRSTUVWX01234567", encoding="utf-8")
    git(repo, "add", "file.txt")
    git(repo, "commit", "-q", "-m", "secret")
    hook = load_hook()
    command = 'git -C "%s" push origin feature' % repo
    assert hook._effective_command_cwd(command) == str(repo)
    diff = hook._push_diff(hook._effective_command_cwd(command))
    assert diff is not None and "AKIA" in diff


def test_commit_still_scans_staged_diff(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "new.txt").write_text("AKIAQRSTUVWX01234567", encoding="utf-8")
    git(repo, "add", "new.txt")
    hook = load_hook()
    verdict = hook.gate_command('cd "%s" && git commit -m secret' % repo)
    assert verdict["action"] == "block"
    assert "staged diff" in verdict["reason"]
