"""The action_gate is a FAIL-CLOSED safety gate, not prose — these tests hold it to that.

We assert it BLOCKS (exit 2) irreversible ops and secret-laden commits, ALLOWS benign commands,
and — the fail-closed property — blocks a commit/push whose diff it cannot scan, while never
bricking ordinary commands.
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(REPO, "hooks", "action_gate.py")


def _check(cmd, cwd=None):
    return subprocess.run([sys.executable, GATE, "check", "--command", cmd],
                          capture_output=True, text=True, cwd=cwd or REPO)


def test_selftest_passes():
    r = subprocess.run([sys.executable, GATE, "selftest"], capture_output=True, text=True, cwd=REPO)
    assert r.returncode == 0, r.stdout
    assert "PASS" in r.stdout


def test_force_push_blocked():
    r = _check("git push --force origin main")
    assert r.returncode == 2, r.stdout
    assert "block" in r.stdout.lower()


def test_history_rewrite_blocked():
    assert _check("git filter-branch --tree-filter x HEAD").returncode == 2


def test_mass_delete_blocked():
    assert _check("rm -rf /").returncode == 2


def test_destructive_sql_blocked():
    assert _check("psql -c 'DROP DATABASE prod'").returncode == 2


def test_benign_commands_allowed():
    # non-push/commit benign commands never trigger the staged-diff scan → deterministic
    assert _check("git status").returncode == 0
    assert _check("rm -f build/tmp.o").returncode == 0
    assert _check("ls -la && grep -rn foo src/").returncode == 0


def _git_repo(tmp_path):
    import subprocess as sp
    d = str(tmp_path)
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        sp.run(["git"] + args, cwd=d, capture_output=True)
    return d


def test_clean_staged_commit_allowed(tmp_path):
    d = _git_repo(tmp_path)
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "ok.py"], cwd=d, capture_output=True)
    assert _check("git commit -m x", cwd=d).returncode == 0


def test_secret_in_staged_commit_blocked(tmp_path):
    d = _git_repo(tmp_path)
    fake_key = "AKIA" + "QRSTUVWX01234567"  # built at runtime so this file stays clean
    (tmp_path / "cfg.py").write_text('AWS = "%s"\n' % fake_key, encoding="utf-8")
    subprocess.run(["git", "add", "cfg.py"], cwd=d, capture_output=True)
    r = _check("git commit -m x", cwd=d)
    assert r.returncode == 2, r.stdout
    assert "secret" in r.stdout.lower()


def test_push_without_git_is_failclosed(tmp_path):
    # a push where the staged diff cannot be read must BLOCK (a check that can't run is not a pass)
    assert _check("git push origin main", cwd=str(tmp_path)).returncode == 2


def test_pretooluse_json_blocks_force_push():
    # The PreToolUse (Bash) hook is project-scoped since v3.10.3 — it fires only inside an active
    # simplicio-loop project (an `.orchestrator/` marker or SIMPLICIO_LOOP=1); elsewhere it no-ops so
    # the command runs unchanged. Exercise the in-project path so the block is deterministic.
    r = subprocess.run([sys.executable, GATE], input='{"tool_input":{"command":"git push -f"}}',
                       capture_output=True, text=True, cwd=REPO,
                       env={**os.environ, "SIMPLICIO_LOOP": "1"})
    assert r.returncode == 2, r.stdout + r.stderr


def test_secret_in_diff_blocks(tmp_path):
    patch = tmp_path / "p.diff"
    fake_key = "AKIA" + "QRSTUVWX01234567"  # built at runtime; no placeholder word, so it's detected
    patch.write_text('+++ b/config.py\n+AWS = "%s"\n' % fake_key, encoding="utf-8")
    r = subprocess.run([sys.executable, GATE, "scan-diff", "--diff", str(patch)],
                       capture_output=True, text=True, cwd=REPO)
    assert r.returncode == 2, r.stdout
    assert "secret" in r.stdout.lower()


def test_placeholder_not_flagged(tmp_path):
    patch = tmp_path / "p.diff"
    patch.write_text('+api_key = "your-api-key-here"\n', encoding="utf-8")
    r = subprocess.run([sys.executable, GATE, "scan-diff", "--diff", str(patch)],
                       capture_output=True, text=True, cwd=REPO)
    assert r.returncode == 0, r.stdout


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_action_gate")
