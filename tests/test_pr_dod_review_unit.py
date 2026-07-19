"""Unit tests for scripts/pr_dod_review.py — PR review against DoD + issue acceptance criteria.

Built so a session that finds all open issues already claimed by other agents can still
contribute value: mechanically checking whether an open PR satisfies CLAUDE.md's 7-dimension
Definition of Done and the issue's own frozen acceptance-criteria checklist, instead of a
vibe-based approval.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pr_dod_review", ROOT / "scripts" / "pr_dod_review.py")
pr_dod_review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pr_dod_review)  # type: ignore[union-attr]


def test_extract_ac_items_parses_checked_and_unchecked():
    body = "## AC\n- [x] done thing\n- [ ] pending thing\n- [X] also done (capital X)\n"
    items = pr_dod_review.extract_ac_items(body)
    assert items == [
        {"text": "done thing", "checked": True},
        {"text": "pending thing", "checked": False},
        {"text": "also done (capital X)", "checked": True},
    ]


def test_extract_ac_items_empty_for_no_checklist():
    assert pr_dod_review.extract_ac_items("just prose, no checklist") == []


def test_review_flags_all_dimensions_missing_for_bare_summary():
    result = pr_dod_review.review("## Summary\nSmall change.\n")
    assert result["verdict"] == "GAPS_FOUND"
    assert "testes_unitarios" in result["missing_dod"]
    assert "cobertura_minima" in result["missing_dod"]


def test_review_compliant_when_all_dimensions_present():
    body = (
        "## Summary\nFeature X.\n\n"
        "Unit tests cover Y. Integration tests hit the real DB. "
        "Ran a full end-to-end system test. Regression suite green. "
        "Benchmark: latency 12ms -> 4ms. Coverage 91%.\n"
    )
    result = pr_dod_review.review(body)
    assert result["verdict"] == "COMPLIANT"
    assert result["missing_dod"] == []


def test_review_allows_explicit_not_applicable():
    body = (
        "## Summary\nDocs-only, no runtime code.\n\n"
        "Not applicable for this PR — no new logic, no cross-service seam, no full run flow, "
        "nothing that could regress, no hot path, nothing to measure.\n"
    )
    result = pr_dod_review.review(body)
    assert result["missing_dod"] == []
    non_impl = [d for d in result["dod"] if d["dimension"] != "implementacao"]
    assert all(d["skipped_with_reason"] for d in non_impl)


def test_review_unresolved_acs_reported():
    issue_body = "## Critérios\n- [x] a\n- [ ] b\n- [ ] c\n"
    result = pr_dod_review.review("## Summary\nx\n", issue_body)
    assert result["ac_items_total"] == 3
    assert result["unresolved_acs"] == ["b", "c"]


def test_review_ac_resolved_when_pr_cites_nearby_evidence():
    issue_body = "## Critérios\n- [ ] handles empty input\n"
    pr_body = "## Summary\nx\n\nhandles empty input -> verified with test_empty.py, PASS.\n"
    result = pr_dod_review.review(pr_body, issue_body)
    assert result["unresolved_acs"] == []


def test_review_ac_not_resolved_by_mere_mention_without_evidence_words():
    issue_body = "## Critérios\n- [ ] handles empty input\n"
    pr_body = "## Summary\nx\n\nWe discuss handles empty input somewhere but say nothing else.\n"
    result = pr_dod_review.review(pr_body, issue_body)
    assert result["unresolved_acs"] == ["handles empty input"]


def test_review_verdict_gaps_found_when_ac_unresolved_even_if_dod_complete():
    complete_dod_body = (
        "## Summary\nx\n\nUnit tests. Integration tests. End-to-end system test. "
        "Regression green. Benchmark improved. Coverage 90%.\n"
    )
    issue_body = "## Critérios\n- [ ] still pending\n"
    result = pr_dod_review.review(complete_dod_body, issue_body)
    assert result["verdict"] == "GAPS_FOUND"
    assert result["unresolved_acs"] == ["still pending"]


def test_render_comment_includes_pr_and_issue_numbers():
    result = pr_dod_review.review("## Summary\nx\n")
    comment = pr_dod_review.render_comment(result, pr_number=42, issue_number=7)
    assert "PR #42" in comment
    assert "issue #7" in comment
    assert "MISSING" in comment


def test_render_comment_compliant_has_no_action_needed_line():
    body = (
        "## Summary\nx\n\nUnit tests. Integration tests. End-to-end system test. "
        "Regression green. Benchmark improved. Coverage 90%.\n"
    )
    result = pr_dod_review.review(body)
    comment = pr_dod_review.render_comment(result)
    assert "No mechanical gaps found" in comment


def test_selftest_passes():
    assert pr_dod_review.cmd_selftest({}) == 0
