"""Repository size budget guard (#294) — proves the guard reports tracked-tree size and FAILS on
a regression, while grandfathering pre-existing oversized files instead of retroactively failing
on history it did not create.

Scope note: this guard measures/gates the CURRENT tracked working tree only. It never touches git
history and never runs `git filter-repo` -- the #294 issue explicitly requires a separate,
maintainer-approved decision (backup, dry-run, communicated window) before any history rewrite.
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD = os.path.join(REPO, "scripts", "repository_budget.py")
BASELINE = os.path.join(REPO, "scripts", "repository_budget_baseline.json")


def _run(args, cwd=None):
    return subprocess.run([sys.executable, GUARD] + args, capture_output=True, text=True,
                          cwd=cwd or REPO, stdin=subprocess.DEVNULL)


def _init_scratch_repo(root):
    """Copy repository_budget.py into a fresh throwaway git repo and return its scripts/ dir."""
    scripts_dir = os.path.join(str(root), "scripts")
    os.makedirs(scripts_dir, exist_ok=True)
    with open(GUARD, "rb") as src, open(os.path.join(scripts_dir, "repository_budget.py"), "wb") as dst:
        dst.write(src.read())
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=str(root), check=True)
    return scripts_dir


def _git_add_all(root):
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)


def test_baseline_is_committed_and_readable():
    assert os.path.exists(BASELINE), "scripts/repository_budget_baseline.json must be committed"
    with open(BASELINE, encoding="utf-8") as f:
        data = json.load(f)
    assert "total_bytes" in data and data["total_bytes"] > 0
    assert "tracked_file_count" in data
    assert "known_oversized_files" in data


def test_report_passes_against_committed_baseline():
    r = _run([])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "repository-budget: PASS" in r.stdout
    assert "largest tracked files" in r.stdout


def test_check_mode_is_quiet_on_pass():
    r = _run(["--check"])
    assert r.returncode == 0
    assert r.stdout.strip() == "", "quiet --check mode should print nothing on a passing run"


def test_scratch_repo_new_baseline_grandfathers_current_oversized_file(tmp_path):
    scripts_dir = _init_scratch_repo(tmp_path)
    # A pre-existing oversized file, present before the baseline is ever written.
    big = tmp_path / "legacy-asset.bin"
    big.write_bytes(b"x" * (3 * 1024 * 1024))
    _git_add_all(tmp_path)

    r = subprocess.run([sys.executable, os.path.join(scripts_dir, "repository_budget.py"),
                        "--update-baseline"], capture_output=True, text=True, cwd=str(tmp_path),
                       stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stdout + r.stderr

    with open(os.path.join(scripts_dir, "repository_budget_baseline.json"), encoding="utf-8") as f:
        baseline = json.load(f)
    assert "legacy-asset.bin" in baseline["known_oversized_files"]

    # Now a plain check must PASS -- the file is grandfathered, not newly flagged.
    r2 = subprocess.run([sys.executable, os.path.join(scripts_dir, "repository_budget.py"),
                        "--check"], capture_output=True, text=True, cwd=str(tmp_path),
                       stdin=subprocess.DEVNULL)
    assert r2.returncode == 0, r2.stdout + r2.stderr


def test_scratch_repo_new_oversized_file_fails(tmp_path):
    scripts_dir = _init_scratch_repo(tmp_path)
    small = tmp_path / "readme.txt"
    small.write_text("hello\n", encoding="utf-8")
    _git_add_all(tmp_path)
    subprocess.run([sys.executable, os.path.join(scripts_dir, "repository_budget.py"),
                    "--update-baseline"], cwd=str(tmp_path), check=True, capture_output=True)

    # Add a brand-new file over the per-file cap AFTER the baseline was written.
    big = tmp_path / "new-video.bin"
    big.write_bytes(b"x" * (3 * 1024 * 1024))
    _git_add_all(tmp_path)

    r = subprocess.run([sys.executable, os.path.join(scripts_dir, "repository_budget.py")],
                       capture_output=True, text=True, cwd=str(tmp_path), stdin=subprocess.DEVNULL)
    assert r.returncode == 1, "a brand-new oversized tracked file must fail the gate: " + r.stdout
    assert "FAIL" in r.stdout
    assert "new-video.bin" in r.stdout


def test_scratch_repo_total_growth_past_threshold_fails(tmp_path):
    scripts_dir = _init_scratch_repo(tmp_path)
    f1 = tmp_path / "data.bin"
    f1.write_bytes(b"x" * (100 * 1024))
    _git_add_all(tmp_path)
    subprocess.run([sys.executable, os.path.join(scripts_dir, "repository_budget.py"),
                    "--update-baseline"], cwd=str(tmp_path), check=True, capture_output=True)

    # Grow the total tracked tree well past the 25% threshold, without any single file crossing
    # the per-file cap, to isolate the total-size regression path.
    f2 = tmp_path / "data2.bin"
    f2.write_bytes(b"x" * (200 * 1024))
    _git_add_all(tmp_path)

    r = subprocess.run([sys.executable, os.path.join(scripts_dir, "repository_budget.py")],
                       capture_output=True, text=True, cwd=str(tmp_path), stdin=subprocess.DEVNULL)
    assert r.returncode == 1, "total tree growth past threshold must fail the gate: " + r.stdout
    assert "FAIL" in r.stdout


def test_selftest_passes():
    r = _run(["selftest"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "repository_budget selftest: PASS" in r.stdout


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_repository_budget")
