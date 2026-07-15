"""pr_evidence.py `comment --publish` (#295 audit finding) — proves the idempotent GitHub comment
publish path BLOCKS clearly instead of silently claiming success when it cannot actually post.

`scripts/pr_evidence.py comment` used to only ever print the evidence comment to stdout — nothing
published it back to the source issue, and nothing verified a post landed (the exact gap flagged
in the #295 audit). `publish_comment`/`find_existing_comment` themselves are exercised in-process
by `python3 scripts/pr_evidence.py selftest` (fake-runner idempotency: create vs. update, and a
raised PublishError on gh failure). This file covers the CLI wiring end of that path via subprocess
so a missing `--issue`/`--repo` (no git remote in a scratch dir) reliably BLOCKS (exit 3) rather
than silently doing nothing while still printing "success"-shaped output — no real `gh`/network
call is ever made here.
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(REPO, "scripts", "pr_evidence.py")


def _run(args, cwd=None):
    return subprocess.run([sys.executable, WORKER] + args, capture_output=True, text=True,
                          cwd=cwd or REPO, stdin=subprocess.DEVNULL)


def test_selftest_covers_publish_idempotency():
    r = _run(["selftest"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "publish.creates_when_absent" in r.stdout
    assert "publish.updates_when_marker_found" in r.stdout
    assert "publish.raises_on_gh_failure" in r.stdout


def test_comment_without_publish_only_prints_to_stdout():
    r = _run(["comment", "--pr", "34"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PR: #34" in r.stdout
    # No publish attempted -> no BLOCKED log line on stderr.
    assert "BLOCKED" not in r.stderr


def test_publish_without_issue_blocks(tmp_path):
    # A scratch cwd with no anchor and no git remote -- --issue cannot be resolved.
    r = _run(["comment", "--pr", "34", "--publish", "--repo", "acme/widgets"], cwd=str(tmp_path))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "BLOCKED" in r.stderr
    assert "--issue" in r.stderr


def test_publish_without_repo_blocks(tmp_path):
    # --issue given, but no --repo -- there is deliberately NO git-remote auto-detect fallback
    # (a "helpful" auto-detect would mean a bare --publish silently targets whatever repo the
    # CWD happens to be in), so this must BLOCK rather than guess.
    r = _run(["comment", "--pr", "34", "--issue", "12", "--publish"], cwd=str(tmp_path))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "BLOCKED" in r.stderr
    assert "--repo" in r.stderr


def test_describe_cli_lists_publish_flags():
    r = _run(["--describe-cli"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--publish" in r.stdout
    assert "--issue" in r.stdout
    assert "--repo" in r.stdout


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_pr_evidence_comment_publish")
