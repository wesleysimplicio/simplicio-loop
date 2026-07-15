"""#283: unit tests for scripts/test_categories.py -- the per-category test-runner split.

Fast, no subprocess spawn except where explicitly noted (`run_category` cases), each capped with
a generous timeout so a slow sandboxed CI box doesn't turn this into a hang.
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

sys.path.insert(0, os.path.join(REPO, "scripts"))
import test_categories as tc  # noqa: E402


def test_categories_tuple_is_the_four_documented_lanes():
    assert tc.CATEGORIES == ("unit", "integration", "system", "regression")


def test_discover_rejects_unknown_category():
    try:
        tc.discover("performance")
    except ValueError as exc:
        assert "unknown category" in str(exc)
    else:
        raise AssertionError("expected ValueError for an unrecognized category")


def test_discover_only_matches_the_exact_suffix(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a_unit.py").write_text("def test_x(): pass\n", encoding="utf-8")
    (tests_dir / "test_b_integration.py").write_text("def test_x(): pass\n", encoding="utf-8")
    (tests_dir / "test_c_plain.py").write_text("def test_x(): pass\n", encoding="utf-8")
    (tests_dir / "not_a_test.py").write_text("x = 1\n", encoding="utf-8")

    unit_files = tc.discover("unit", tests_dir=str(tests_dir))
    assert [os.path.basename(f) for f in unit_files] == ["test_a_unit.py"]

    integration_files = tc.discover("integration", tests_dir=str(tests_dir))
    assert [os.path.basename(f) for f in integration_files] == ["test_b_integration.py"]

    uncategorized = tc.discover_uncategorized(tests_dir=str(tests_dir))
    # not_a_test.py excluded: no test_ prefix (mirrors pytest's own default collection rule)
    assert [os.path.basename(f) for f in uncategorized] == ["test_c_plain.py"]


def test_discover_and_uncategorized_partition_every_real_test_file_exactly_once():
    all_files = tc._list_test_files(tc.TESTS_DIR)
    per_category = {c: tc.discover(c) for c in tc.CATEGORIES}
    uncategorized = tc.discover_uncategorized()
    accounted = sum(len(v) for v in per_category.values()) + len(uncategorized)
    assert accounted == len(all_files)

    seen = set()
    for files in per_category.values():
        for f in files:
            assert f not in seen
            seen.add(f)
    for f in uncategorized:
        assert f not in seen


def test_run_category_reports_blocked_with_zero_files_and_never_a_false_pass(tmp_path):
    verdict = tc.run_category("unit", repo=str(tmp_path))
    assert verdict["status"] == "blocked"
    assert verdict["ok"] is False
    assert verdict["files"] == []
    assert verdict["schema"] == "simplicio.test-category-gate/v1"


def test_run_category_system_lane_passes_for_real(tmp_path):
    # `system` currently has exactly one real, fast file in this repo -- exercise the real thing.
    report_path = tmp_path / "system-report.json"
    proc = subprocess.run(
        [PY, os.path.join(REPO, "scripts", "test_categories.py"), "run",
         "--category", "system", "--emit-json", str(report_path)],
        cwd=REPO, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
    )
    assert proc.returncode == 0
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["category"] == "system"
    assert payload["status"] == "pass"
    assert payload["ok"] is True
    assert payload["files"] == ["tests/test_quality_matrix_system.py"]


def test_cli_status_reports_all_five_buckets():
    proc = subprocess.run(
        [PY, os.path.join(REPO, "scripts", "test_categories.py"), "status"],
        cwd=REPO, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15,
    )
    assert proc.returncode == 0
    report = json.loads(proc.stdout)
    assert set(report) == {"unit", "integration", "system", "regression", "uncategorized", "total"}
    assert report["total"] == sum(v for k, v in report.items() if k != "total")


def test_cli_list_uncategorized_matches_python_api():
    proc = subprocess.run(
        [PY, os.path.join(REPO, "scripts", "test_categories.py"), "list", "--category", "uncategorized"],
        cwd=REPO, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15,
    )
    assert proc.returncode == 0
    listed = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    assert listed == tc.discover_uncategorized()


def test_selftest_cli_passes():
    proc = subprocess.run(
        [PY, os.path.join(REPO, "scripts", "test_categories.py"), "selftest"],
        cwd=REPO, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15,
    )
    assert proc.returncode == 0
    assert "selftest OK" in proc.stdout


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_test_categories_unit")
