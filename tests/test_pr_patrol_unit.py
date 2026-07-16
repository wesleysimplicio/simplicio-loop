"""Offline unit tests for the every-two-items / final / post-merge PR patrol."""
import json
import subprocess

from simplicio_loop.pr_patrol import PrPatrol, patrol_due


class Runner:
    def __init__(self, stdout):
        self.stdout = stdout
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, self.stdout, "")


def test_patrol_due_every_two_and_at_final_or_post_merge():
    assert patrol_due(1)["due"] is False
    assert patrol_due(2) == {"due": True, "reason": "cadence", "cadence": 2, "completed_items": 2}
    assert patrol_due(3)["due"] is False
    assert patrol_due(4)["due"] is True
    assert patrol_due(1, final=True)["reason"] == "final_reconciliation"
    assert patrol_due(0, post_merge=True)["reason"] == "post_merge"


def test_patrol_classifies_review_ci_rebase_and_conflict_work():
    rows = [
        {"number": 1, "url": "https://x/1", "headRefName": "a", "baseRefName": "main",
         "isDraft": False, "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY",
         "reviewDecision": "", "statusCheckRollup": []},
        {"number": 2, "url": "https://x/2", "headRefName": "b", "baseRefName": "main",
         "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "BEHIND",
         "reviewDecision": "CHANGES_REQUESTED",
         "statusCheckRollup": [{"conclusion": "FAILURE"}]},
        {"number": 3, "url": "https://x/3", "headRefName": "c", "baseRefName": "main",
         "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
         "reviewDecision": "APPROVED", "statusCheckRollup": [{"conclusion": "SUCCESS"}]},
    ]
    runner = Runner(json.dumps(rows))
    report = PrPatrol("o/r", runner=runner).inspect(completed_items=2)
    assert [row["number"] for row in report["action_required"]] == [1, 2]
    assert set(report["action_required"][1]["signals"]) == {
        "REBASE_REQUIRED", "REVIEW_CHANGES_REQUESTED", "CHECKS_FAILED"}
    assert report["clean"] == [3]
    assert "list" in runner.calls[0]


def test_patrol_does_not_call_github_when_cadence_is_not_due():
    runner = Runner("[]")
    report = PrPatrol("o/r", runner=runner).inspect(completed_items=1)
    assert report["reason"] == "not_due"
    assert runner.calls == []
