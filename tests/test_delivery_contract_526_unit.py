"""Unit tests for scripts/delivery_contract.py (issue #526 Etapa 4 — delivery contract).

Covers: strict schema validation (unknown field = error, not silence), freeze/force semantics
(mirrors task_anchor.py's goal re-anchor), the new-file baseline guard (real git repos), the
comment linter for Python/C#/TS/JS (line + block + new-docstring detection), the commit-message
convention check, and the final-report compliance renderer.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts import delivery_contract as dc

VALID_CONTRACT = {
    "open_pr": False,
    "push_branch": True,
    "allow_new_files_in_repo": False,
    "allow_comments_in_code": False,
    "commit_message_convention": "#<issue> - <type>: <desc>",
}


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "a@b.c")
    git(repo, "config", "user.name", "tester")
    (repo / "existing.txt").write_text("hello\n", encoding="utf-8")
    git(repo, "add", "existing.txt")
    git(repo, "commit", "-q", "-m", "init")
    return repo


# ----- schema validation -------------------------------------------------------------------

def test_valid_contract_has_no_errors():
    assert dc.validate(VALID_CONTRACT) == []


def test_unknown_field_is_a_hard_error():
    bad = dict(VALID_CONTRACT, extra_field=True)
    errors = dc.validate(bad)
    assert any("extra_field" in e for e in errors)


def test_missing_required_field_is_an_error():
    bad = dict(VALID_CONTRACT)
    del bad["allow_comments_in_code"]
    errors = dc.validate(bad)
    assert any("allow_comments_in_code" in e for e in errors)


def test_wrong_type_is_an_error():
    bad = dict(VALID_CONTRACT, open_pr="false")
    errors = dc.validate(bad)
    assert any("open_pr" in e for e in errors)


def test_blank_commit_message_convention_is_an_error():
    bad = dict(VALID_CONTRACT, commit_message_convention="   ")
    errors = dc.validate(bad)
    assert any("commit_message_convention" in e for e in errors)


def test_non_object_is_an_error():
    assert dc.validate([1, 2, 3]) != []


def test_load_contract_file_missing_raises(tmp_path):
    try:
        dc.load_contract_file(str(tmp_path / "nope.json"))
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_load_contract_file_valid_json(tmp_path):
    path = tmp_path / "delivery.json"
    path.write_text(json.dumps(VALID_CONTRACT), encoding="utf-8")
    assert dc.load_contract_file(str(path)) == VALID_CONTRACT


# ----- freeze / force semantics ------------------------------------------------------------

def test_freeze_with_no_existing_contract():
    frozen, err = dc.freeze(None, VALID_CONTRACT)
    assert err is None
    assert frozen == VALID_CONTRACT


def test_refreeze_identical_contract_needs_no_force():
    frozen, err = dc.freeze(VALID_CONTRACT, VALID_CONTRACT, force=False)
    assert err is None
    assert frozen == VALID_CONTRACT


def test_refreeze_different_contract_without_force_is_blocked():
    changed = dict(VALID_CONTRACT, open_pr=True)
    frozen, err = dc.freeze(VALID_CONTRACT, changed, force=False)
    assert frozen is None
    assert err is not None and "force" in err


def test_refreeze_different_contract_with_force_succeeds():
    changed = dict(VALID_CONTRACT, open_pr=True)
    frozen, err = dc.freeze(VALID_CONTRACT, changed, force=True)
    assert err is None
    assert frozen["open_pr"] is True


# ----- new-file baseline guard (real git) --------------------------------------------------

def test_baseline_capture_and_clean_check(tmp_path):
    repo = make_repo(tmp_path)
    baseline_path = tmp_path / "baseline.json"
    dc.capture_baseline(str(repo), str(baseline_path))
    result = dc.check_new_files(str(repo), str(baseline_path))
    assert result["ok"] is True
    assert result["violations"] == []


def test_new_untracked_file_is_a_violation(tmp_path):
    repo = make_repo(tmp_path)
    baseline_path = tmp_path / "baseline.json"
    dc.capture_baseline(str(repo), str(baseline_path))
    (repo / "FooTests.cs").write_text("// test file\n", encoding="utf-8")
    result = dc.check_new_files(str(repo), str(baseline_path))
    assert result["ok"] is False
    assert "FooTests.cs" in result["violations"]


def test_baseline_file_already_present_is_not_a_violation(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "Pending.txt").write_text("was already there\n", encoding="utf-8")
    baseline_path = tmp_path / "baseline.json"
    dc.capture_baseline(str(repo), str(baseline_path))  # captured AFTER Pending.txt appeared
    result = dc.check_new_files(str(repo), str(baseline_path))
    assert result["ok"] is True  # Pending.txt was already in the baseline snapshot


def test_missing_baseline_fails_closed(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "New.txt").write_text("x\n", encoding="utf-8")
    result = dc.check_new_files(str(repo), str(tmp_path / "never-captured.json"))
    assert result["ok"] is False
    assert "New.txt" in result["violations"]


def test_new_file_guard_none_when_no_delivery_contract(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "New.txt").write_text("x\n", encoding="utf-8")
    assert dc.new_file_guard({}, root=str(repo)) is None
    assert dc.new_file_guard(None, root=str(repo)) is None


def test_new_file_guard_none_when_new_files_allowed(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "New.txt").write_text("x\n", encoding="utf-8")
    anchor = {"delivery": {"allow_new_files_in_repo": True}}
    assert dc.new_file_guard(anchor, root=str(repo)) is None


def test_new_file_guard_blocks_with_clear_reason(tmp_path):
    repo = make_repo(tmp_path)
    baseline_path = tmp_path / "baseline.json"
    dc.capture_baseline(str(repo), str(baseline_path))
    (repo / "FooTests.cs").write_text("// test file\n", encoding="utf-8")
    anchor = {"delivery": {"allow_new_files_in_repo": False}}
    reason = dc.new_file_guard(anchor, root=str(repo), baseline_path=str(baseline_path))
    assert reason is not None
    assert "FooTests.cs" in reason
    assert "allow_new_files_in_repo" in reason


# ----- comment linter -----------------------------------------------------------------------

def _diff(path, hunk_header, body_lines):
    return (
        "diff --git a/%s b/%s\n" % (path, path)
        + "index 111..222 100644\n"
        + "--- a/%s\n" % path
        + "+++ b/%s\n" % path
        + hunk_header + "\n"
        + "\n".join(body_lines) + "\n"
    )


def test_python_line_comment_detected():
    diff_text = _diff("foo.py", "@@ -1,2 +1,3 @@",
                       [" def f():", "+    # a new comment", "     return 1"])
    violations = dc.find_added_comment_lines(diff_text)
    assert len(violations) == 1
    assert violations[0]["file"] == "foo.py"
    assert violations[0]["line"] == 2
    assert "comment" in violations[0]["text"]


def test_python_new_docstring_detected():
    diff_text = _diff("foo.py", "@@ -1,1 +1,3 @@",
                       [" def f():", '+    """New docstring."""', "     return 1"])
    violations = dc.find_added_comment_lines(diff_text)
    assert len(violations) == 1
    assert '"""' in violations[0]["text"]


def test_python_added_code_line_is_clean():
    diff_text = _diff("foo.py", "@@ -1,1 +1,2 @@", [" def f():", "+    x = 1"])
    assert dc.find_added_comment_lines(diff_text) == []


def test_csharp_line_comment_detected():
    diff_text = _diff("Foo.cs", "@@ -1,1 +1,3 @@",
                       [" public void Foo() {", "+    // inline comment", "     return;"])
    violations = dc.find_added_comment_lines(diff_text)
    assert len(violations) == 1
    assert violations[0]["file"] == "Foo.cs"


def test_csharp_block_comment_open_and_close_detected():
    diff_text = _diff("Foo.cs", "@@ -1,1 +1,4 @@",
                       [" public void Foo() {", "+    /* block", "+       comment */",
                        "     return;"])
    violations = dc.find_added_comment_lines(diff_text)
    assert len(violations) == 2


def test_typescript_line_comment_detected():
    diff_text = _diff("foo.ts", "@@ -1,1 +1,3 @@",
                       [" function f() {", "+    // inline comment", "     return 1;"])
    violations = dc.find_added_comment_lines(diff_text)
    assert len(violations) == 1
    assert violations[0]["file"] == "foo.ts"


def test_javascript_added_code_line_is_clean():
    diff_text = _diff("foo.js", "@@ -1,1 +1,2 @@", [" function f() {", "+    return 1;"])
    assert dc.find_added_comment_lines(diff_text) == []


def test_unknown_extension_is_never_flagged():
    diff_text = _diff("foo.md", "@@ -1,1 +1,2 @@", [" # Title", "+ // not really a comment here"])
    assert dc.find_added_comment_lines(diff_text) == []


def test_comment_guard_none_when_allowed():
    assert dc.comment_guard({"delivery": {"allow_comments_in_code": True}}) is None
    assert dc.comment_guard({}) is None


def test_lint_working_diff_via_real_git_staged(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "existing.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "existing.py")
    git(repo, "commit", "-q", "-m", "add py file")
    (repo / "existing.py").write_text("x = 1\n# a sneaky comment\n", encoding="utf-8")
    git(repo, "add", "existing.py")
    result = dc.lint_working_diff(str(repo), cached=True)
    assert result["ok"] is False
    assert result["violations"]


def test_lint_working_diff_clean_when_no_comment_added(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "existing.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "existing.py")
    git(repo, "commit", "-q", "-m", "add py file")
    (repo / "existing.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    git(repo, "add", "existing.py")
    result = dc.lint_working_diff(str(repo), cached=True)
    assert result["ok"] is True
    assert result["violations"] == []


# ----- commit message convention -------------------------------------------------------------

def test_commit_message_matches_convention():
    convention = "#<issue> - <type>: <desc>"
    assert dc.commit_message_matches("#526 - feat: add delivery contract", convention) is True


def test_commit_message_missing_issue_hash_fails():
    convention = "#<issue> - <type>: <desc>"
    assert dc.commit_message_matches("526 - feat: add delivery contract", convention) is False


def test_commit_message_wrong_shape_fails():
    convention = "#<issue> - <type>: <desc>"
    assert dc.commit_message_matches("#526 feat add delivery contract", convention) is False


def test_blank_convention_matches_anything():
    assert dc.commit_message_matches("anything at all", "") is True


# ----- compliance report ----------------------------------------------------------------------

def test_compliance_report_no_contract():
    report = dc.render_compliance_report({})
    assert "no delivery contract frozen" in report


def test_compliance_report_all_permissive_clauses():
    anchor = {"delivery": {"open_pr": True, "push_branch": True,
                          "allow_new_files_in_repo": True, "allow_comments_in_code": True,
                          "commit_message_convention": "x"}}
    report = dc.render_compliance_report(anchor)
    assert "open_pr: true" in report
    assert "no restriction" in report


def test_compliance_report_flags_new_file_violation(tmp_path):
    repo = make_repo(tmp_path)
    baseline_path = tmp_path / "baseline.json"
    dc.capture_baseline(str(repo), str(baseline_path))
    (repo / "FooTests.cs").write_text("// x\n", encoding="utf-8")
    anchor = {"delivery": dict(VALID_CONTRACT)}
    report = dc.render_compliance_report(anchor, root=str(repo), baseline_path=str(baseline_path))
    assert "VIOLATION" in report
    assert "FooTests.cs" in report


def test_compliance_report_commit_message_clause():
    anchor = {"delivery": dict(VALID_CONTRACT)}
    ok_report = dc.render_compliance_report(
        anchor, last_commit_message="#526 - feat: add delivery contract")
    assert "compliant" in ok_report
    bad_report = dc.render_compliance_report(anchor, last_commit_message="oops")
    assert "VIOLATION" in bad_report
