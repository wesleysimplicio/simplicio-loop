"""Unit tests for scripts/worktree_cleanup.py (#484 post-merge cleanup)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import worktree_cleanup as wc  # noqa: E402


PORCELAIN_FIXTURE = (
    "worktree /repo\n"
    "HEAD aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
    "branch refs/heads/main\n"
    "\n"
    "worktree /repo/.claude/worktrees/feature-x\n"
    "HEAD bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
    "branch refs/heads/feature-x\n"
    "\n"
    "worktree /repo/.claude/worktrees/other-task\n"
    "HEAD cccccccccccccccccccccccccccccccccccccccc\n"
    "branch refs/heads/other-task\n"
)


# ----- find_worktree_for_branch (pure parser) -----------------------------------------------

def test_find_worktree_for_branch_matches_target():
    assert wc.find_worktree_for_branch(PORCELAIN_FIXTURE, "feature-x") == \
        "/repo/.claude/worktrees/feature-x"


def test_find_worktree_for_branch_does_not_match_others():
    assert wc.find_worktree_for_branch(PORCELAIN_FIXTURE, "other-task") == \
        "/repo/.claude/worktrees/other-task"


def test_find_worktree_for_branch_no_match_returns_none():
    assert wc.find_worktree_for_branch(PORCELAIN_FIXTURE, "no-such-branch") is None


def test_find_worktree_for_branch_empty_text():
    assert wc.find_worktree_for_branch("", "feature-x") is None
    assert wc.find_worktree_for_branch(PORCELAIN_FIXTURE, "") is None


def test_find_worktree_for_branch_detached_worktree_has_no_branch():
    porcelain = PORCELAIN_FIXTURE + (
        "\nworktree /repo/.claude/worktrees/detached\n"
        "HEAD dddddddddddddddddddddddddddddddddddddddd\n"
        "detached\n"
    )
    assert wc.find_worktree_for_branch(porcelain, "detached") is None


# ----- decide_cleanup (pure decision function) -----------------------------------------------

def test_decide_cleanup_skips_when_pr_not_merged():
    result = wc.decide_cleanup(False, "feature-x", "/repo/wt", False)
    assert result == {"action": "skip", "reason": "pr_not_merged"}


def test_decide_cleanup_skips_both_on_uncommitted_changes():
    result = wc.decide_cleanup(True, "feature-x", "/repo/wt", True)
    assert result["action"] == "skip"
    assert result["reason"] == "uncommitted_changes"
    assert result["path"] == "/repo/wt"


def test_decide_cleanup_allows_branch_delete_when_no_worktree():
    result = wc.decide_cleanup(True, "feature-x", None, False)
    assert result["action"] == "cleanup"
    assert result["delete_worktree"] is False
    assert result["delete_branch"] is True


def test_decide_cleanup_cleans_both_when_safe():
    result = wc.decide_cleanup(True, "feature-x", "/repo/wt", False)
    assert result["action"] == "cleanup"
    assert result["delete_worktree"] is True
    assert result["delete_branch"] is True


# ----- check_pr_merged (isolated fetch) --------------------------------------------------------

def test_check_pr_merged_true_when_state_merged_and_has_mergedat():
    def fake_fetch(repo, pr_number):
        return {"state": "MERGED", "mergedAt": "2026-07-17T00:00:00Z", "headRefName": "feature-x"}

    result = wc.check_pr_merged("owner/repo", 484, fetch=fake_fetch)
    assert result == {"merged": True, "head_ref": "feature-x"}


def test_check_pr_merged_false_when_open():
    def fake_fetch(repo, pr_number):
        return {"state": "OPEN", "mergedAt": None, "headRefName": "feature-x"}

    result = wc.check_pr_merged("owner/repo", 484, fetch=fake_fetch)
    assert result == {"merged": False, "head_ref": "feature-x"}


def test_check_pr_merged_false_when_closed_unmerged():
    def fake_fetch(repo, pr_number):
        return {"state": "CLOSED", "mergedAt": None, "headRefName": "feature-x"}

    result = wc.check_pr_merged("owner/repo", 484, fetch=fake_fetch)
    assert result["merged"] is False


# ----- cleanup() orchestration: dry-run never deletes, real run deletes once -------------------

def _fake_calls():
    return {"remove_worktree": 0, "delete_local": 0, "delete_remote": 0}


def _wire(calls, porcelain=PORCELAIN_FIXTURE, status="", fetch=None):
    def fake_fetch(repo, pr_number):
        return {"state": "MERGED", "mergedAt": "2026-07-17T00:00:00Z", "headRefName": "feature-x"}

    def fake_remove_worktree(path):
        calls["remove_worktree"] += 1

    def fake_delete_local(branch):
        calls["delete_local"] += 1

    def fake_delete_remote(branch):
        calls["delete_remote"] += 1

    return dict(
        fetch=fetch or fake_fetch,
        worktree_list_fn=lambda: porcelain,
        status_fn=lambda path: status,
        remove_worktree_fn=fake_remove_worktree,
        delete_local_branch_fn=fake_delete_local,
        delete_remote_branch_fn=fake_delete_remote,
    )


def test_cleanup_dry_run_never_calls_delete_functions():
    calls = _fake_calls()
    result = wc.cleanup("owner/repo", 484, "feature-x", dry_run=True, **_wire(calls))
    assert result["decision"]["action"] == "cleanup"
    assert calls == {"remove_worktree": 0, "delete_local": 0, "delete_remote": 0}
    assert result["would_do"]


def test_cleanup_real_run_calls_delete_functions_exactly_once():
    calls = _fake_calls()
    result = wc.cleanup("owner/repo", 484, "feature-x", dry_run=False, **_wire(calls))
    assert result["decision"]["action"] == "cleanup"
    assert calls == {"remove_worktree": 1, "delete_local": 1, "delete_remote": 1}
    assert result["actions_taken"] == ["removed_worktree", "deleted_local_branch", "deleted_remote_branch"]


def test_cleanup_skip_not_merged_never_calls_delete_functions():
    calls = _fake_calls()

    def fake_fetch_open(repo, pr_number):
        return {"state": "OPEN", "mergedAt": None, "headRefName": "feature-x"}

    result = wc.cleanup("owner/repo", 484, "feature-x", dry_run=False,
                        **_wire(calls, fetch=fake_fetch_open))
    assert result["decision"] == {"action": "skip", "reason": "pr_not_merged"}
    assert calls == {"remove_worktree": 0, "delete_local": 0, "delete_remote": 0}


def test_cleanup_skip_uncommitted_never_calls_delete_functions():
    calls = _fake_calls()
    result = wc.cleanup("owner/repo", 484, "feature-x", dry_run=False,
                        **_wire(calls, status="M dirty.py\n"))
    assert result["decision"]["action"] == "skip"
    assert result["decision"]["reason"] == "uncommitted_changes"
    assert calls == {"remove_worktree": 0, "delete_local": 0, "delete_remote": 0}


def test_cleanup_no_worktree_still_deletes_branch():
    calls = _fake_calls()
    # branch not present in the porcelain fixture -> find_worktree_for_branch returns None
    result = wc.cleanup("owner/repo", 484, "ghost-branch", dry_run=False, **_wire(calls))
    assert result["decision"]["action"] == "cleanup"
    assert result["decision"]["delete_worktree"] is False
    assert calls["remove_worktree"] == 0
    assert calls["delete_local"] == 1
    assert calls["delete_remote"] == 1


def test_worktree_cleanup_selftest_passes():
    # In-process (no subprocess spawn) so this doesn't depend on the host being able to fork a
    # child process; `cmd_selftest` calls sys.exit(0) on success, which pytest surfaces as
    # SystemExit.
    import pytest as _pytest

    with _pytest.raises(SystemExit) as excinfo:
        wc.cmd_selftest({})
    assert excinfo.value.code == 0
