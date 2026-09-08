"""Unit tests for scripts/coordinator.py — the multi-agent decision core (#467/#468 phase-1 slice).

Covers claim extraction, merged-PR detection, the four decision actions (OWN/CONTINUE_OWN/
DEFER_ACTIVE_CLAIM/RECLAIM_STALE/VERIFY_PARTIAL), and the duplicate_risk collision flag — the exact
scenario observed live in this repo (two sessions building competing modules for the same issue).
"""
import importlib.util

import json
import sys
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


def test_issue_reference_does_not_match_larger_issue_number():
    prs = [{"number": 1, "state": "MERGED", "body": "fixes #1232", "title": "#1232", "merged_at": NOW}]
    assert coordinator.has_merged_pr_referencing(12, prs) is False
    assert coordinator.has_active_delivery_pr_referencing(12, [{"state": "OPEN", "body": "fixes #1232", "title": "#1232"}]) is False


def test_historical_merged_pr_is_not_active_delivery_ownership():
    prs = [{"number": 1, "state": "MERGED", "body": "fixes #1232", "title": "x",
            "merged_at": NOW - HOUR}]
    decision = coordinator.decide_for_issue(1232, [], prs, "follow-up")
    assert decision["action"] == "VERIFY_PARTIAL"
    assert decision["historical_merged_pr"] is True
    assert decision["active_delivery_pr"] is False


def test_open_delivery_pr_is_reported_as_active():
    prs = [{"number": 2, "state": "OPEN", "body": "fixes #1232", "title": "x"}]
    decision = coordinator.decide_for_issue(1232, [], prs, "follow-up")
    assert decision["historical_merged_pr"] is False
    assert decision["active_delivery_pr"] is True


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


@pytest.mark.parametrize("value, expected", [
    (12, 12), ("12", 12), (" 12 ", 12),
    (0, None), (-1, None), (12.5, None), (True, None), (None, None), ("12.5", None),
])
def test_canonical_issue_number_rejects_non_integer_or_non_positive(value, expected):
    assert coordinator._canonical_issue_number(value) == expected


@pytest.mark.parametrize("invalid", [None, 12.5, "12.5", True, 0])
def test_decide_invalid_issue_number_fails_closed(invalid):
    decision = coordinator.decide_for_issue(
        invalid, [], [{"state": "MERGED", "body": "#12", "merged_at": NOW}], "self"
    )
    assert decision["action"] == "INVALID_INPUT"
    assert decision["issue"] == invalid
    assert decision["has_merged_pr"] is False


def _invoke_cli(monkeypatch, capsys, *args):
    import sys
    monkeypatch.setattr(sys, "argv", ["coordinator.py", *args])
    with pytest.raises(SystemExit) as exc_info:
        coordinator.main()
    return exc_info.value.code, capsys.readouterr()


def test_cli_decide_snapshot_invalid_numbers_are_unverified(tmp_path, monkeypatch, capsys):
    import json
    snapshot = {"issues": [{}, {"number": 12.5}], "prs": [{"state": "MERGED", "body": "#12", "merged_at": NOW}]}
    path = tmp_path / "invalid-numbers.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    code, output = _invoke_cli(monkeypatch, capsys, "decide", "--snapshot-file", str(path))
    rows = [json.loads(line[len("UNVERIFIED|"):]) for line in output.out.splitlines()]
    assert code == 0
    assert len(rows) == 2
    assert all(row["action"] == "INVALID_INPUT" for row in rows)
    assert output.err == ""


def test_cli_snapshot_decide_entrypoint(tmp_path, monkeypatch, capsys):
    import json
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"issues": [{"number": 7}], "prs": []}), encoding="utf-8")
    code, output = _invoke_cli(monkeypatch, capsys, "decide", "--snapshot-file", str(path))
    assert code == 0
    assert output.out.startswith("UNVERIFIED|")
    assert json.loads(output.out[len("UNVERIFIED|"):])["action"] == "OWN"


def test_cli_describe_and_parser_errors(monkeypatch, capsys):
    code, output = _invoke_cli(monkeypatch, capsys, "--describe-cli")
    assert code == 0
    assert "decide" in json.loads(output.out)["verbs"]
    code, output = _invoke_cli(monkeypatch, capsys)
    assert code == 2 and "coordinator decision core" in output.out
    code, output = _invoke_cli(monkeypatch, capsys, "unknown")
    assert code == 2 and "unknown command" in output.out


def test_cli_survey_missing_and_invalid_args(monkeypatch, capsys):
    code, output = _invoke_cli(monkeypatch, capsys, "survey")
    assert code == 2 and "--repo and --issues are required" in output.out
    code, output = _invoke_cli(monkeypatch, capsys, "survey", "--repo", "acme/repo", "--issues", "12.5")
    assert code == 2 and "comma-separated list of integers" in output.out


def test_cli_survey_fail_open_when_gh_unavailable(monkeypatch, capsys):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("gh")
    monkeypatch.setattr(coordinator.subprocess, "run", unavailable)
    code, output = _invoke_cli(monkeypatch, capsys, "survey", "--repo", "acme/repo", "--issues", "1")
    payload = json.loads(output.out)
    assert code == 0
    assert payload["status"] == "UNVERIFIED"
    assert payload["reason_code"] == "gh_unavailable"


def test_cli_survey_measured_snapshot(monkeypatch, capsys):
    import json
    from types import SimpleNamespace
    def fake_run(argv, **kwargs):
        if argv[1:3] == ["pr", "list"]:
            stdout = json.dumps([{"number": 9, "state": "OPEN", "body": "#7", "title": "x"}])
        else:
            stdout = json.dumps({"number": 7, "title": "item", "state": "open", "comments": []})
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
    monkeypatch.setattr(coordinator.subprocess, "run", fake_run)
    code, output = _invoke_cli(monkeypatch, capsys, "survey", "--repo", "acme/repo", "--issues", "7")
    payload = json.loads(output.out)
    assert code == 0
    assert payload["status"] == "MEASURED"
    assert payload["issues"][0]["number"] == 7


def test_selftest_failure_reports_unverified(monkeypatch, capsys):
    original = coordinator.decide_for_issue
    calls = {"count": 0}
    def fail_first(*args, **kwargs):
        result = dict(original(*args, **kwargs))
        if calls["count"] == 0:
            result["action"] = "BROKEN"
        calls["count"] += 1
        return result
    monkeypatch.setattr(coordinator, "decide_for_issue", fail_first)
    assert coordinator.cmd_selftest({}) == 1
    assert "UNVERIFIED|coordinator selftest" in capsys.readouterr().out
