"""Unit tests for scripts/deep_correctness_gate.py — the mechanical subset of DOD.md's Layer 1
(universal) + Layer 2 (risk-surface) checks, per issue #579.

Loaded by path (matches the convention already used by tests/test_pr_dod_review_unit.py) so this
runs standalone under `python3 tests/test_deep_correctness_gate_unit.py`, under pytest, and via
`scripts/check.py`'s auto-discovery of `tests/test_*.py` without any package install.
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "deep_correctness_gate", ROOT / "scripts" / "deep_correctness_gate.py")
dcg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dcg)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Layer 1.3 — regression test on a `fix:` commit
# ---------------------------------------------------------------------------

def test_regression_check_not_applicable_without_a_fix_commit():
    ok, detail, meta = dcg.check_regression_test(["feat: add a widget"], "")
    assert ok is True
    assert meta["applicable"] is False
    assert "not applicable" in detail


def test_regression_check_fails_when_fix_commit_has_no_diff_at_all():
    ok, _detail, meta = dcg.check_regression_test(["fix: stop corrupting output"], "")
    assert ok is False
    assert meta["applicable"] is True


def test_regression_check_fails_when_fix_touches_only_source():
    diff = (
        "diff --git a/mechanical_edit.py b/mechanical_edit.py\n"
        "+++ b/mechanical_edit.py\n"
        "+    honor_order = True\n"
    )
    ok, detail, meta = dcg.check_regression_test(["fix: honor explicit order"], diff)
    assert ok is False
    assert meta["test_files_touched"] == []
    assert "regression test" in detail


def test_regression_check_passes_when_fix_adds_lines_to_a_test_file():
    diff = (
        "diff --git a/mechanical_edit.py b/mechanical_edit.py\n"
        "+++ b/mechanical_edit.py\n"
        "+    honor_order = True\n"
        "diff --git a/tests/test_mechanical_edit.py b/tests/test_mechanical_edit.py\n"
        "+++ b/tests/test_mechanical_edit.py\n"
        "+def test_out_of_order_plan_is_rejected():\n"
        "+    assert True\n"
    )
    ok, _detail, meta = dcg.check_regression_test(["fix: honor explicit order"], diff)
    assert ok is True
    assert meta["test_files_touched"] == ["tests/test_mechanical_edit.py"]


def test_regression_check_test_file_with_only_deletions_still_fails():
    # A test file appearing in the diff with NO added lines (pure deletion) must not count as
    # "a regression test was added" — this is exactly the gap that would let a fix ship with a
    # weakened, not strengthened, test suite.
    diff = "diff --git a/tests/test_x.py b/tests/test_x.py\n+++ b/tests/test_x.py\n"
    ok, _detail, meta = dcg.check_regression_test(["fix: something"], diff)
    assert ok is False
    # the file IS reported as "touched" (useful for diagnosis) even though it contributed no
    # added lines -- the gate cares about added content, not mere presence in the diff.
    assert meta["test_files_touched"] == ["tests/test_x.py"]


def test_regression_check_recognizes_scoped_and_breaking_fix_variants():
    for msg in ["fix: plain", "fix(mapper): scoped", "fix(mapper)!: scoped and breaking"]:
        ok, _detail, meta = dcg.check_regression_test([msg], "")
        assert meta["applicable"] is True, msg
        assert ok is False, msg  # no diff supplied -> can't verify -> fails


def test_regression_check_ignores_non_fix_conventional_commits():
    for msg in ["feat: add", "chore: bump", "refactor: rename", "docs: update readme"]:
        ok, _detail, meta = dcg.check_regression_test([msg], "")
        assert meta["applicable"] is False, msg
        assert ok is True, msg


def test_parse_diff_file_blocks_splits_multiple_files():
    diff = (
        "diff --git a/a.py b/a.py\n+++ b/a.py\n+x = 1\n-old\n"
        "diff --git a/b/c.py b/b/c.py\n+++ b/b/c.py\n+y = 2\n"
    )
    blocks = dcg.parse_diff_file_blocks(diff)
    assert [p for p, _ in blocks] == ["a.py", "b/c.py"]
    assert blocks[0][1] == ["x = 1"]  # the "-old" deletion line is not an added line
    assert blocks[1][1] == ["y = 2"]


# ---------------------------------------------------------------------------
# Layer 2.3 — invariant question answered in the PR body
# ---------------------------------------------------------------------------

def test_invariant_check_skipped_when_no_pr_body_supplied():
    ok, _detail, meta = dcg.check_invariant_answer("")
    assert ok is True
    assert meta["applicable"] is False


def test_invariant_check_fails_when_section_missing():
    ok, _detail, meta = dcg.check_invariant_answer("## Summary\nSome change.\n")
    assert ok is False
    assert meta["applicable"] is True


def test_invariant_check_fails_when_section_is_empty():
    body = "## Summary\nx\n\n## Invariant\n\n## Test plan\ny\n"
    ok, _detail, _meta = dcg.check_invariant_answer(body)
    assert ok is False


def test_invariant_check_passes_with_english_heading_and_content():
    body = "## Invariant\nYes, both partition by file path.\n"
    ok, _detail, meta = dcg.check_invariant_answer(body)
    assert ok is True
    assert "partition by file path" in meta["answer"]


def test_invariant_check_passes_with_portuguese_heading():
    body = "## Invariante\nNao, uma particiona por arquivo e outra pelo plano inteiro.\n"
    ok, _detail, _meta = dcg.check_invariant_answer(body)
    assert ok is True


def test_invariant_check_accepts_explicit_not_applicable_answer():
    # An explicit "n/a" is a real answer to the question, not a silent skip — the check is about
    # presence of an explicit response, not that a dual-processing invariant must always exist.
    body = "## Invariant\nn/a — no dual-processing code touched by this change.\n"
    ok, _detail, _meta = dcg.check_invariant_answer(body)
    assert ok is True


def test_invariant_section_stops_at_the_next_header():
    body = "## Invariant\nBoth use the same key.\n\n## Something Else\nunrelated content\n"
    _ok, _detail, meta = dcg.check_invariant_answer(body)
    assert "unrelated content" not in meta["answer"]


# ---------------------------------------------------------------------------
# Layer 1.4 — coverage gate enforced in CI (not just documented)
# ---------------------------------------------------------------------------

def test_coverage_gate_check_fails_with_no_workflows_directory():
    with tempfile.TemporaryDirectory() as tmp:
        ok, _detail, meta = dcg.check_coverage_gate_workflow(tmp)
        assert ok is False
        assert meta["workflows_scanned"] == []


def test_coverage_gate_check_fails_with_empty_workflows_directory():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, ".github", "workflows"))
        ok, _detail, meta = dcg.check_coverage_gate_workflow(tmp)
        assert ok is False
        assert meta["workflows_scanned"] == []


def test_coverage_gate_check_fails_when_no_workflow_has_a_signal():
    with tempfile.TemporaryDirectory() as tmp:
        wf = os.path.join(tmp, ".github", "workflows")
        os.makedirs(wf)
        with open(os.path.join(wf, "ci.yml"), "w", encoding="utf-8") as f:
            f.write("name: ci\njobs:\n  build:\n    steps:\n      - run: echo hi\n")
        ok, _detail, meta = dcg.check_coverage_gate_workflow(tmp)
        assert ok is False
        assert meta["workflows_scanned"] == ["ci.yml"]


def test_coverage_gate_check_passes_when_a_workflow_has_cov_fail_under():
    with tempfile.TemporaryDirectory() as tmp:
        wf = os.path.join(tmp, ".github", "workflows")
        os.makedirs(wf)
        with open(os.path.join(wf, "python-ci.yml"), "w", encoding="utf-8") as f:
            f.write("jobs:\n  test:\n    steps:\n"
                    "      - run: pytest --cov=pkg --cov-fail-under=88\n")
        ok, _detail, meta = dcg.check_coverage_gate_workflow(tmp)
        assert ok is True
        assert meta["workflows_with_gate"] == ["python-ci.yml"]


def test_coverage_gate_check_recognizes_codecov_action_signal():
    with tempfile.TemporaryDirectory() as tmp:
        wf = os.path.join(tmp, ".github", "workflows")
        os.makedirs(wf)
        with open(os.path.join(wf, "ci.yaml"), "w", encoding="utf-8") as f:
            f.write("jobs:\n  test:\n    steps:\n      - uses: codecov/codecov-action@v4\n")
        ok, _detail, _meta = dcg.check_coverage_gate_workflow(tmp)
        assert ok is True


def test_coverage_gate_check_ignores_non_workflow_files():
    with tempfile.TemporaryDirectory() as tmp:
        wf = os.path.join(tmp, ".github", "workflows")
        os.makedirs(wf)
        with open(os.path.join(wf, "README.md"), "w", encoding="utf-8") as f:
            f.write("--cov-fail-under=90 mentioned here but not a workflow file\n")
        ok, _detail, meta = dcg.check_coverage_gate_workflow(tmp)
        assert ok is False
        assert meta["workflows_scanned"] == []


# ---------------------------------------------------------------------------
# build_verdict / render_text — composition
# ---------------------------------------------------------------------------

def test_build_verdict_fails_overall_when_any_applicable_check_fails():
    with tempfile.TemporaryDirectory() as tmp:
        v = dcg.build_verdict(tmp, ["fix: bug"], "", "")
        assert v["ok"] is False
        assert v["schema"] == "simplicio.deep-correctness-gate/v1"
        assert len(v["checks"]) == 3


def test_build_verdict_passes_when_nothing_applicable_and_ci_has_a_gate():
    with tempfile.TemporaryDirectory() as tmp:
        wf = os.path.join(tmp, ".github", "workflows")
        os.makedirs(wf)
        with open(os.path.join(wf, "ci.yml"), "w", encoding="utf-8") as f:
            f.write("jobs:\n  test:\n    steps:\n"
                    "      - run: pytest --cov=pkg --cov-fail-under=88\n")
        v = dcg.build_verdict(tmp, ["chore: bump deps"], "", "")
        assert v["ok"] is True


def test_render_text_reports_pass_and_lists_every_check():
    with tempfile.TemporaryDirectory() as tmp:
        wf = os.path.join(tmp, ".github", "workflows")
        os.makedirs(wf)
        with open(os.path.join(wf, "ci.yml"), "w", encoding="utf-8") as f:
            f.write("jobs:\n  test:\n    steps:\n      - run: pytest --cov-fail-under=90\n")
        v = dcg.build_verdict(tmp, [], "", "")
        rendered = dcg.render_text(v)
        assert "deep-correctness-gate: PASS" in rendered
        assert all(c["id"] in rendered for c in v["checks"])


def test_render_text_reports_fail_when_verdict_is_not_ok():
    with tempfile.TemporaryDirectory() as tmp:
        v = dcg.build_verdict(tmp, ["fix: bug"], "", "")
        rendered = dcg.render_text(v)
        assert "deep-correctness-gate: FAIL" in rendered


# ---------------------------------------------------------------------------
# CLI surface — --describe-cli, main() dispatch, selftest
# ---------------------------------------------------------------------------

def test_describe_cli_returns_zero_and_lists_verbs(capsys):
    rc = dcg.main(["--describe-cli"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"check"' in out
    assert '"selftest"' in out


def test_main_check_json_matches_build_verdict(capsys, tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(
        "jobs:\n  test:\n    steps:\n      - run: pytest --cov-fail-under=88\n", encoding="utf-8")
    rc = dcg.main(["check", "--repo-root", str(tmp_path), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"schema": "simplicio.deep-correctness-gate/v1"' in out
    assert '"ok": true' in out


def test_main_check_reads_diff_and_pr_body_from_file(capsys, tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(
        "jobs:\n  test:\n    steps:\n      - run: pytest --cov-fail-under=88\n", encoding="utf-8")
    diff_path = tmp_path / "change.diff"
    diff_path.write_text(
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n+def test_regression():\n+    assert True\n",
        encoding="utf-8",
    )
    rc = dcg.main([
        "check", "--repo-root", str(tmp_path), "--diff", "@%s" % diff_path,
        "--commit-msg", "fix: real bug", "--json",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"ok": true' in out


def test_main_with_no_subcommand_prints_help_and_exits_nonzero(capsys):
    rc = dcg.main([])
    assert rc == 2


def test_selftest_passes():
    assert dcg.cmd_selftest(None) == 0


if __name__ == "__main__":
    # Zero-dependency self-run path (scripts/check.py falls back to this when pytest isn't
    # installed) -- run every top-level test_* function directly.
    import inspect

    mod = sys.modules[__name__]
    failures = []
    for name, fn in sorted(inspect.getmembers(mod, inspect.isfunction)):
        if not name.startswith("test_"):
            continue
        sig = inspect.signature(fn)
        if sig.parameters:
            continue  # skip fixture-dependent tests (capsys/tmp_path) in the bare-python fallback
        try:
            fn()
            print("ok - %s" % name)
        except Exception as e:  # noqa: BLE001
            failures.append((name, e))
            print("FAIL - %s: %s" % (name, e))
    if failures:
        sys.exit(1)
    print("all bare-python tests passed")
