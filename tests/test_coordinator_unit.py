"""Unit tests for scripts/coordinator.py — the multi-agent decision core (#467/#468 phase-1 slice).

Covers claim extraction, merged-PR detection, the four decision actions (OWN/CONTINUE_OWN/
DEFER_ACTIVE_CLAIM/RECLAIM_STALE/VERIFY_PARTIAL), and the duplicate_risk collision flag — the exact
scenario observed live in this repo (two sessions building competing modules for the same issue).
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("coordinator", ROOT / "scripts" / "coordinator.py")
coordinator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(coordinator)  # type: ignore[union-attr]

NOW = 1_800_000_000.0
HOUR = 3600.0


def _claim_comment(branch, ts, camel_case=False):
    body = f"🔒 **Claimed** — working via `/simplicio-loop` on branch `{branch}`."
    key = "createdAt" if camel_case else "created_at"
    return {"body": body, key: ts}


def test_extract_claims_finds_branch_and_sorts_oldest_first():
    comments = [
        _claim_comment("branch-b", NOW - HOUR),
        _claim_comment("branch-a", NOW - 2 * HOUR),
        {"body": "unrelated comment with no claim marker", "created_at": NOW},
    ]
    claims = coordinator.extract_claims(comments)
    assert [c[0] for c in claims] == ["branch-a", "branch-b"]


def test_extract_claims_accepts_gh_camel_case_created_at():
    comments = [_claim_comment("branch-a", NOW - HOUR, camel_case=True)]
    claims = coordinator.extract_claims(comments)
    assert claims == [("branch-a", NOW - HOUR)]


def test_has_merged_pr_referencing_matches_body_and_title():
    prs = [{"number": 1, "state": "MERGED", "body": "fixes #466", "title": "x", "merged_at": NOW}]
    assert coordinator.has_merged_pr_referencing(466, prs) is True
    assert coordinator.has_merged_pr_referencing(999, prs) is False


def test_has_merged_pr_referencing_ignores_open_prs():
    prs = [{"number": 1, "state": "OPEN", "body": "fixes #466", "title": "x"}]
    assert coordinator.has_merged_pr_referencing(466, prs) is False


def test_has_merged_pr_referencing_respects_after_ts():
    prs = [{"number": 1, "state": "MERGED", "body": "#466", "title": "", "merged_at": NOW - 5 * HOUR}]
    assert coordinator.has_merged_pr_referencing(466, prs, after_ts=NOW - HOUR) is False
    assert coordinator.has_merged_pr_referencing(466, prs, after_ts=NOW - 10 * HOUR) is True


def test_decide_own_when_untouched():
    d = coordinator.decide_for_issue(1, [], [], "self", now=NOW)
    assert d["action"] == "OWN"
    assert d["duplicate_risk"] is False


def test_decide_continue_own_when_self_is_latest_claimant():
    comments = [_claim_comment("self", NOW - HOUR)]
    d = coordinator.decide_for_issue(1, comments, [], "self", now=NOW)
    assert d["action"] == "CONTINUE_OWN"


def test_decide_defers_to_fresh_foreign_claim():
    comments = [_claim_comment("other", NOW - HOUR)]
    d = coordinator.decide_for_issue(1, comments, [], "self", now=NOW, stale_hours=6.0)
    assert d["action"] == "DEFER_ACTIVE_CLAIM"


def test_decide_reclaims_stale_foreign_claim():
    comments = [_claim_comment("other", NOW - 10 * HOUR)]
    d = coordinator.decide_for_issue(1, comments, [], "self", now=NOW, stale_hours=6.0)
    assert d["action"] == "RECLAIM_STALE"


def test_decide_verify_partial_when_merged_pr_but_issue_still_open():
    comments = [_claim_comment("other", NOW - 10 * HOUR)]
    prs = [{"number": 1, "state": "MERGED", "body": "#1", "title": "", "merged_at": NOW - 5 * HOUR}]
    d = coordinator.decide_for_issue(1, comments, prs, "self", now=NOW, stale_hours=6.0)
    assert d["action"] == "VERIFY_PARTIAL"
    assert d["has_merged_pr"] is True


def test_decide_verify_partial_beats_own_when_no_claims_but_pr_merged():
    prs = [{"number": 1, "state": "MERGED", "body": "#1", "title": "", "merged_at": NOW - HOUR}]
    d = coordinator.decide_for_issue(1, [], prs, "self", now=NOW)
    assert d["action"] == "VERIFY_PARTIAL"


def test_decide_flags_duplicate_risk_for_near_simultaneous_foreign_claims():
    comments = [_claim_comment("branch-a", NOW - 2 * HOUR), _claim_comment("branch-b", NOW - 1.5 * HOUR)]
    d = coordinator.decide_for_issue(1, comments, [], "self", now=NOW, collision_window_hours=2.0)
    assert d["duplicate_risk"] is True


def test_decide_no_duplicate_risk_when_claims_far_apart():
    comments = [_claim_comment("branch-a", NOW - 20 * HOUR), _claim_comment("branch-b", NOW - HOUR)]
    d = coordinator.decide_for_issue(1, comments, [], "self", now=NOW, collision_window_hours=2.0)
    assert d["duplicate_risk"] is False


def test_decide_no_duplicate_risk_for_same_branch_reclaiming():
    comments = [_claim_comment("branch-a", NOW - 2 * HOUR), _claim_comment("branch-a", NOW - 1.5 * HOUR)]
    d = coordinator.decide_for_issue(1, comments, [], "self", now=NOW, collision_window_hours=2.0)
    assert d["duplicate_risk"] is False


def test_real_world_466_scenario():
    """The exact live collision this module was built to catch: this session's PR merged for
    #466, but a sibling branch claimed the same issue afterward without knowing about the merge."""
    comments = [
        {"body": "earlier unrelated audit comment", "created_at": NOW - 8 * HOUR},
        _claim_comment("claude/simplicio-loop-skill-issues-0c53a9", NOW - HOUR),
    ]
    prs = [{"number": 475, "state": "MERGED", "body": "feat(#466): phase-1 slice",
           "title": "feat(#466)", "merged_at": NOW - 3 * HOUR}]
    d = coordinator.decide_for_issue(466, comments, prs, "claude/simplicio-loop-skill-issues-4cff87",
                                     now=NOW, stale_hours=6.0)
    assert d["action"] == "VERIFY_PARTIAL"
    assert d["has_merged_pr"] is True


def test_selftest_passes():
    assert coordinator.cmd_selftest({}) == 0


def test_cmd_decide_reads_snapshot_file_and_tags_measured(tmp_path, capsys):
    import json
    snapshot = {
        "issues": [{"number": 1, "comments": [_claim_comment("self", NOW - HOUR)]}],
        "prs": [],
    }
    snapshot_file = tmp_path / "snapshot.json"
    snapshot_file.write_text(json.dumps(snapshot), encoding="utf-8")
    coordinator.cmd_decide({"snapshot-file": str(snapshot_file), "self-branch": "self"})
    out = capsys.readouterr().out.strip()
    assert out.startswith("MEASURED|")
    payload = json.loads(out[len("MEASURED|"):])
    assert payload["action"] == "CONTINUE_OWN"


def test_cmd_decide_rejects_invalid_json(tmp_path, capsys):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not json", encoding="utf-8")
    with pytest.raises(SystemExit) as exc_info:
        coordinator.cmd_decide({"snapshot-file": str(bad_file)})
    assert exc_info.value.code == 2
    out = capsys.readouterr().out
    assert "UNVERIFIED|" in out
