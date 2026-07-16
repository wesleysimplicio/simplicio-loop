"""Offline unit tests for the every-two-items / final / post-merge PR patrol."""
import json
import subprocess

from simplicio_loop.pr_patrol import (
    ACCEPTANCE_REVIEW_MARKER,
    PrPatrol,
    assess_acceptance_criteria,
    patrol_due,
    render_acceptance_review_comment,
)


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


def test_acceptance_review_never_accepts_missing_or_unevidenced_criteria():
    assert assess_acceptance_criteria("## Acceptance criteria\n\n- [x] Works\n")["eligible_for_accepted"] is False
    packet = {"pr": {"number": 7, "head_sha": "abc"},
              "acceptance": assess_acceptance_criteria("## Acceptance criteria\n\n- [x] Works\n")}
    try:
        render_acceptance_review_comment(packet, "ACCEPTED", note="looked good")
        raise AssertionError("expected acceptance evidence gate")
    except ValueError as exc:
        assert "fully checked" in str(exc)


class ReviewRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        command = " ".join(argv)
        if "pr view" in command:
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                "number": 7, "url": "https://x/7", "title": "feature", "headRefOid": "abc",
                "baseRefOid": "def", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                "reviewDecision": "", "statusCheckRollup": [],
                "body": "## Acceptance criteria\n\n- [x] Works — _evidence:_ `pytest`\n",
            }), "")
        if "issues/7/comments" in command and "--paginate" in command:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if "issues/7/comments" in command and "--method POST" in command:
            body = json.loads(kwargs["input"])["body"]
            return subprocess.CompletedProcess(argv, 0, json.dumps({"id": 99, "body": body}), "")
        raise AssertionError("unexpected command %s" % command)


def test_publish_acceptance_review_is_idempotent_marker_receipt_not_approval():
    runner = ReviewRunner()
    result = PrPatrol("o/r", runner=runner).publish_acceptance_review(7, "ACCEPTED", note="diff and pytest receipt checked")
    assert result["comment_id"] == 99
    assert result["verdict"] == "ACCEPTED"
    posted = [kw["input"] for argv, kw in runner.calls if "--method POST" in " ".join(argv)][0]
    assert ACCEPTANCE_REVIEW_MARKER in posted
    assert "human approval" in posted
