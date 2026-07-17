"""Unit tests for scripts/pr_dod_review.py — WI-485.

Covers the 7-dimension DoD verdict, unresolved-AC extraction, ready_to_merge
gating, and comment rendering. Mirrors the embedded selftest but as a pytest
suite so the project's coverage tool picks it up.
"""
import importlib.util
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "pr_dod_review.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("pr_dod_review", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


PR_FULL = (
    "## Implementação\nUnit tests via pytest. Integration tests with no mocks. "
    "System e2e. Regression: existing suite green. Benchmark latency measured. "
    "Coverage 90%."
)
ISSUE_OPEN = "## AC\n- [x] done one\n- [ ] pending two\n- [ ] pending three"


def test_all_seven_dimensions_detected(mod):
    v = mod.build_verdict(PR_FULL, ISSUE_OPEN)
    assert v["dod_addressed"] == "7/7"
    for dim in mod.DOD_DIMENSIONS:
        assert v["dod"][dim]["addressed"] is True


def test_unresolved_acs_extracted(mod):
    v = mod.build_verdict(PR_FULL, ISSUE_OPEN)
    acs = v["unresolved_acceptance_criteria"]
    assert len(acs) == 2
    assert "pending two" in acs
    assert "pending three" in acs


def test_resolved_ac_not_counted(mod):
    v = mod.build_verdict(PR_FULL, ISSUE_OPEN)
    assert "[x] done one" not in v["unresolved_acceptance_criteria"]


def test_not_ready_when_acs_open(mod):
    v = mod.build_verdict(PR_FULL, ISSUE_OPEN)
    assert v["ready_to_merge"] is False


def test_ready_when_full_and_closed(mod):
    full_issue = "## AC\n- [x] a\n- [x] b"
    v = mod.build_verdict(PR_FULL, full_issue)
    assert v["dod_addressed"] == "7/7"
    assert v["ready_to_merge"] is True


def test_dimension_missing_when_signal_absent(mod):
    v = mod.build_verdict("no signals here", "no signals")
    assert v["dod_addressed"] == "0/7"
    for dim in mod.DOD_DIMENSIONS:
        assert v["dod"][dim]["addressed"] is False


def test_render_comment_has_table_and_unresolved(mod):
    v = mod.build_verdict(PR_FULL, ISSUE_OPEN)
    cm = mod.render_comment(v)
    assert "| Dimension |" in cm
    assert "- [ ] pending two" in cm
    assert "PR DoD + ACs Review" in cm


def test_render_comment_all_resolved(mod):
    full_issue = "## AC\n- [x] a\n- [x] b"
    v = mod.build_verdict(PR_FULL, full_issue)
    cm = mod.render_comment(v)
    assert "All acceptance criteria resolved" in cm


def test_post_comment_parses_url(mod):
    ok, msg = mod._post_comment(
        "https://github.com/o/r/pull/123", "body"
    )
    # URL parsed and gh invoked (or absent) — must not crash, must report failure.
    assert ok is False
    assert isinstance(msg, str) and len(msg) > 0


def test_post_comment_bad_url(mod):
    ok, msg = mod._post_comment("not-a-url", "body")
    assert ok is False
    assert "cannot parse" in msg


def test_selftest_passes(mod):
    assert mod._selftest() == 0


def test_cli_check_emits_json(mod, capsys):
    rc = mod.main(["check", "--pr-body", PR_FULL, "--issue-body", ISSUE_OPEN])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"schema": "simplicio.pr-dod-review/v1"' in out or '"schema":"simplicio.pr-dod-review/v1"' in out
