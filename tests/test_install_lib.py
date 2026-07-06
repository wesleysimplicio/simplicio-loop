"""#78: coverage for scripts/install_lib.py — the core installer behind install.sh/ps1.

SAFETY: install_lib.py's `ensure_operators()` does a REAL `pip install` unless
--skip-operators is passed, `install_all_deps()` (skipped by --minimal) does a REAL full-stack
pip install, and `setup_monitor()` (also skipped by --minimal) wires OS services. Every
invocation below passes BOTH --skip-operators AND --minimal, always uses --target <tmp_path>
(never --global, never the real HOME), and restricts PATH to a directory with no `simplicio`/
`az` binaries so `ensure_runtime_bind()` can never shell out to a real `simplicio install
--global` even if one happened to be on the host PATH. No real pip install, no real network
call, and no write outside tmp_path happens anywhere in this file.
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_LIB = os.path.join(REPO, "scripts", "install_lib.py")
SKILLS = ["simplicio-tasks", "simplicio-loop", "simplicio-orient",
          "simplicio-review", "simplicio-compress", "simplicio-learn"]


def _safe_env(tmp_home):
    """A PATH with no simplicio*/az binaries + a throwaway HOME, so nothing this script does
    can touch the real host, even in the ensure_runtime_bind() / _link_operator_bins() fallback
    paths that walk HOME-relative candidate directories."""
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:/bin"  # core utils only — no pip/uv/simplicio/az shims
    env["HOME"] = str(tmp_home)
    return env


def _install(runtime, target, tmp_home, extra_args=None):
    args = [sys.executable, INSTALL_LIB, runtime, "--target", str(target),
            "--skip-operators", "--minimal"] + (extra_args or [])
    return subprocess.run(args, capture_output=True, text=True, cwd=str(target),
                          env=_safe_env(tmp_home), timeout=120)


def test_install_claude_copies_skills_and_hooks_and_wires_stop(tmp_path):
    target = tmp_path / "claude_target"
    target.mkdir()
    r = _install("claude", target, tmp_path / "home1")
    assert r.returncode == 0, r.stdout + r.stderr

    skills_root = target / ".claude" / "skills"
    for s in SKILLS:
        assert (skills_root / s).is_dir(), "%s not copied" % s

    hooks_root = target / "hooks"
    assert hooks_root.is_dir()
    assert (hooks_root / "loop_stop.py").is_file()
    assert not (hooks_root / "learn_stop.py").exists(), \
        "learn_stop.py was removed this session — must not be copied"

    settings_path = target / ".claude" / "settings.json"
    assert settings_path.is_file()
    settings = json.loads(settings_path.read_text())
    stop_hooks = settings["hooks"]["Stop"]
    commands = [h["command"] for grp in stop_hooks for h in grp["hooks"]]
    assert any("loop_stop.py" in c for c in commands)
    assert not any("learn_stop.py" in c for c in commands)


def test_install_second_runtime_cursor(tmp_path):
    target = tmp_path / "cursor_target"
    target.mkdir()
    r = _install("cursor", target, tmp_path / "home2")
    assert r.returncode == 0, r.stdout + r.stderr
    skills_root = target / ".claude" / "skills"
    for s in SKILLS:
        assert (skills_root / s).is_dir()
    hooks_root = target / "hooks"
    assert hooks_root.is_dir() and (hooks_root / "loop_stop.py").is_file()
    # cursor wires via hooks/hooks.json convention, not .claude/settings.json
    assert "Cursor format" in r.stdout or "hooks.json" in r.stdout


def test_install_codex_writes_agents_md_entry_block(tmp_path):
    target = tmp_path / "codex_target"
    target.mkdir()
    r = _install("codex", target, tmp_path / "home3")
    assert r.returncode == 0, r.stdout + r.stderr
    agents_md = target / "AGENTS.md"
    assert agents_md.is_file()
    text = agents_md.read_text()
    assert "<!-- simplicio-loop:begin -->" in text
    assert "<!-- simplicio-loop:end -->" in text
    assert "/simplicio-loop" in text


def test_install_idempotent_no_duplicate_hook_entries(tmp_path):
    target = tmp_path / "idempotent_target"
    target.mkdir()
    home = tmp_path / "home4"
    r1 = _install("claude", target, home)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    r2 = _install("claude", target, home)
    assert r2.returncode == 0, r2.stdout + r2.stderr

    settings = json.loads((target / ".claude" / "settings.json").read_text())
    stop_groups = settings["hooks"]["Stop"]
    stop_commands = [h["command"] for grp in stop_groups for h in grp["hooks"]
                     if "loop_stop.py" in h["command"]]
    assert len(stop_commands) == 1, "loop_stop.py must be wired exactly once: %r" % stop_commands

    pretool_groups = settings.get("hooks", {}).get("PreToolUse", [])
    rewrite_commands = [h["command"] for grp in pretool_groups for h in grp.get("hooks", [])
                        if "orient_rewrite.py" in h["command"]]
    assert len(rewrite_commands) <= 1, \
        "orient_rewrite.py must not be duplicated across runs: %r" % rewrite_commands

    # skills/hooks copies must also not error or corrupt on a second pass
    for s in SKILLS:
        assert (target / ".claude" / "skills" / s).is_dir()


def test_install_unknown_runtime_clean_error(tmp_path):
    target = tmp_path / "bogus_target"
    target.mkdir()
    r = _install("not-a-real-runtime", target, tmp_path / "home5")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "unknown runtime" in r.stdout.lower()
    assert "Traceback" not in r.stderr, "unknown runtime must be a clean error, not a crash"


def test_install_never_passes_global_and_target_is_isolated(tmp_path):
    # Belt-and-suspenders: prove the target dir is genuinely isolated — nothing lands outside it.
    target = tmp_path / "isolated_target"
    target.mkdir()
    home = tmp_path / "home6"
    home.mkdir()
    r = _install("claude", target, home)
    assert r.returncode == 0, r.stdout + r.stderr
    # the throwaway HOME must remain untouched by the install (skills/hooks go to --target, not ~)
    assert not (home / ".claude").exists(), \
        "a --target install must never also write into HOME"


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_install_lib")
