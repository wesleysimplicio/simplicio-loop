"""CLI-level coverage for `scripts/stage_report.py` (#433/#442) — proves `preview`/`publish
--dry-run`/`selftest` behave via subprocess, and that `publish` BLOCKS (exit 3) rather than
silently doing nothing when the target repo/issue/pr aren't resolvable. No real `gh`/network call
is ever made here (`selftest` drives a fake runner in-process; the other verbs either render
without publishing or explicitly opt into --dry-run)."""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(REPO, "scripts", "stage_report.py")


def _run(args, cwd=None):
    return subprocess.run([sys.executable, WORKER] + args, capture_output=True, text=True,
                          cwd=cwd or REPO, stdin=subprocess.DEVNULL)


def test_selftest_passes():
    r = _run(["selftest"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "selftest: PASS" in r.stdout


def test_preview_renders_without_network():
    r = _run(["preview", "--run-id", "r1", "--item", "T1", "--stage", "review",
              "--name", "Claude", "--role", "Implementer", "--model", "claude-sonnet-5",
              "--status", "PASS", "--issue", "12", "--pr", "34"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "stage-report:v1 run=r1 item=T1" in r.stdout
    assert "**PASS**" in r.stdout
    assert "Issue #12" in r.stdout and "PR #34" in r.stdout


def test_preview_rejects_bad_status():
    r = _run(["preview", "--run-id", "r1", "--item", "T1", "--stage", "review",
              "--status", "NOT-REAL"])
    assert r.returncode == 3, r.stdout + r.stderr
    assert "BLOCKED" in r.stderr


def test_publish_without_targets_is_dry_run():
    r = _run(["publish", "--run-id", "r1", "--item", "T1", "--stage", "review",
              "--status", "PASS"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "dry-run" in r.stderr
    assert "stage-report:v1" in r.stdout


def test_publish_with_issue_but_no_repo_blocks(tmp_path):
    r = _run(["publish", "--run-id", "r1", "--item", "T1", "--stage", "review",
              "--status", "PASS", "--issue", "12"], cwd=str(tmp_path))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "BLOCKED" in r.stderr
    assert "--repo" in r.stderr


def test_describe_cli_lists_verbs_and_flags():
    r = _run(["--describe-cli"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "preview" in r.stdout and "publish" in r.stdout and "selftest" in r.stdout
    assert "--status" in r.stdout and "--repo" in r.stdout


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_stage_report_cli_integration")
