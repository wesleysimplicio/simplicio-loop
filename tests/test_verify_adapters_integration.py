"""Integration test for `scripts/verify_adapters.py` — installer + filesystem contract, together.

Runs the real install pipeline (`install_lib.py`) into a throwaway target and checks that the
adapter's landed artifacts (skills, entry file with marker, hooks, settings.json wiring) actually
satisfy the contract — multiple components (installer, filesystem, marker format) exercised as a
whole, distinct from a pure-function unit test. We only exercise the "claude" runtime here (fast,
deterministic); the full 11-runtime sweep is `python3 scripts/verify_adapters.py` itself.
"""
import importlib.util
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIFY_ADAPTERS = os.path.join(REPO, "scripts", "verify_adapters.py")

_spec = importlib.util.spec_from_file_location("verify_adapters_test", VERIFY_ADAPTERS)
verify_adapters = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_adapters)


def _run(args):
    return subprocess.run([sys.executable, VERIFY_ADAPTERS] + args, capture_output=True,
                          text=True, cwd=REPO, timeout=120, stdin=subprocess.DEVNULL)


def test_claude_adapter_install_contract_passes():
    r = _run(["claude"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS  claude" in r.stdout, r.stdout


def test_unknown_runtime_is_a_clean_error():
    r = _run(["not-a-real-runtime"])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "unknown runtime" in r.stdout.lower()


def test_tier1_shortcut_expands_to_claude_codex_cursor():
    r = _run(["tier1"])
    assert r.returncode == 0, r.stdout + r.stderr
    for rt in ("claude", "codex", "cursor"):
        assert ("PASS  %-12s" % rt) in r.stdout or ("FAIL  %-12s" % rt) in r.stdout, \
            "tier1 must attempt %s:\n%s" % (rt, r.stdout)


def _seed_skills_dir(target, loop_skill_body):
    skill_dir = os.path.join(target, ".claude", "skills", "simplicio-loop")
    os.makedirs(skill_dir, exist_ok=True)
    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(loop_skill_body)
    for s in verify_adapters.install_lib.SKILLS:
        d = os.path.join(target, ".claude", "skills", s)
        os.makedirs(d, exist_ok=True)
        if s == "simplicio-tasks":
            with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write("# alias\n")


def test_check_flags_missing_turn_header_contract_in_copied_skill(tmp_path):
    """#303 AC1 — a corrupted/stale skill copy (missing the turn-header contract string) must
    be caught by `_check()`, not silently pass."""
    target = str(tmp_path)
    _seed_skills_dir(target, "# simplicio-loop\n\nNo turn-header contract here.\n")
    fails = verify_adapters._check("claude", target)
    assert any("turn-header contract" in f for f in fails), fails


def test_check_passes_when_skill_carries_turn_header_contract(tmp_path):
    """Positive control for the above — the real repo's SKILL.md (copied verbatim) must satisfy
    the new check, proving it isn't a tautological/always-fail assertion."""
    real_skill = os.path.join(REPO, ".claude", "skills", "simplicio-loop", "SKILL.md")
    with open(real_skill, encoding="utf-8") as fsrc:
        content = fsrc.read()
    assert "render --turn-header" in content  # sanity: the real skill DOES carry the contract
    target = str(tmp_path)
    _seed_skills_dir(target, content)
    fails = verify_adapters._check("claude", target)
    assert not any("turn-header contract" in f for f in fails), fails


def test_check_flags_hook_missing_progress_injection_marker(tmp_path):
    """#303 AC1/AC6 — a hook-bound runtime (claude/cursor) with a STALE loop_stop.py (no
    `_progress_header_prefix`) must be flagged, not silently accepted."""
    target = str(tmp_path)
    _seed_skills_dir(target, "placeholder")
    hooks_dir = verify_adapters.install_lib.hooks_dir(target, False)
    os.makedirs(hooks_dir, exist_ok=True)
    with open(os.path.join(hooks_dir, "loop_stop.py"), "w", encoding="utf-8") as f:
        f.write("# a stale stop hook with no progress injection\n")
    fails = verify_adapters._check("claude", target)
    assert any("progress injection" in f for f in fails), fails


def test_check_passes_with_the_real_loop_stop_hook(tmp_path):
    target = str(tmp_path)
    _seed_skills_dir(target, "placeholder")
    hooks_dir = verify_adapters.install_lib.hooks_dir(target, False)
    os.makedirs(hooks_dir, exist_ok=True)
    real_hook = os.path.join(REPO, "hooks", "loop_stop.py")
    with open(real_hook, encoding="utf-8") as fsrc:
        hook_body = fsrc.read()
    assert "_progress_header_prefix" in hook_body  # sanity
    with open(os.path.join(hooks_dir, "loop_stop.py"), "w", encoding="utf-8") as f:
        f.write(hook_body)
    fails = verify_adapters._check("claude", target)
    assert not any("progress injection" in f for f in fails), fails


def test_check_flags_missing_loop_progress_worker(tmp_path):
    """#303 AC5 — a target with skills+hooks landed but NO `scripts/` copied (the pre-fix
    behavior: `install_lib.py` never copied workers into a foreign target) must be flagged by
    `_check()`, not silently pass. Proves the new assertion isn't tautological/always-green."""
    target = str(tmp_path)
    _seed_skills_dir(target, "placeholder")
    fails = verify_adapters._check("claude", target)
    assert any("#303 AC5" in f for f in fails), fails


def test_check_passes_when_loop_progress_selftest_runs_green_from_target(tmp_path):
    """#303 AC5 — positive control: once `scripts/` is copied into the installed target (as the
    real `install_lib.copy_scripts` now does), `loop_progress.py selftest` must run GREEN from
    INSIDE that target (cwd=target, not the source repo) and the dedicated check must accept it."""
    target = str(tmp_path)
    _seed_skills_dir(target, "placeholder")
    verify_adapters.install_lib.copy_scripts(target, False)
    fails = verify_adapters._check_loop_progress_selftest(target)
    assert fails == [], fails


def test_progress_md_updates_with_zero_host_specific_code(tmp_path):
    """#303 AC7 — the "universal denominator" proof: a completely unadapted/unknown runtime
    (no adapter, no hook, nothing host-specific) still gets a correct, live PROGRESS.md/
    progress.json simply by running scripts/loop_progress.py directly — the N3 fallback."""
    progress_script = os.path.join(REPO, "scripts", "loop_progress.py")
    env = dict(os.environ)
    env.update({
        "SIMPLICIO_PROGRESS_DIR": str(tmp_path),
        "SIMPLICIO_ANCHOR_FILE": str(tmp_path / "anchor.json"),
        "SIMPLICIO_BACKLOG_FILE": str(tmp_path / "backlog.jsonl"),
    })
    r = subprocess.run([sys.executable, progress_script, "emit", "--step", "operate",
                       "--status", "begin"], capture_output=True, text=True, cwd=str(tmp_path),
                      env=env, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stdout + r.stderr
    md_path = tmp_path / "PROGRESS.md"
    json_path = tmp_path / "progress.json"
    assert md_path.exists() and json_path.exists()
    snap = json.loads(json_path.read_text(encoding="utf-8"))
    assert snap["step"] == "operate"
    assert "simplicio-loop" in md_path.read_text(encoding="utf-8")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_verify_adapters_integration")
