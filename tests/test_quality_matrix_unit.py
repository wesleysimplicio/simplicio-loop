"""Unit tests for the quality-matrix module itself (#278) — no CLI, no oracle wiring."""
import json
import sys

from simplicio_loop.quality_matrix import (
    DEFAULT_COVERAGE_THRESHOLD,
    QualityMatrixError,
    REQUIRED_REQUIREMENTS,
    build_quality_matrix_template,
    evaluate_quality_matrix,
    validate_coverage_threshold,
)


def _passing_receipt(**overrides):
    receipt = {
        "schema": "simplicio.quality-matrix/v1",
        "coverage_threshold": 85,
        "requirements": {
            name: {"status": "pass", "proof_ref": f"tests/{name}"} for name in REQUIRED_REQUIREMENTS
        },
        "coverage": {"measured": 90.0},
    }
    receipt.update(overrides)
    return receipt


def test_default_threshold_is_eighty_five():
    assert DEFAULT_COVERAGE_THRESHOLD == 85.0


def test_all_required_lanes_present():
    assert set(REQUIRED_REQUIREMENTS) == {
        "implementation", "unit", "integration", "system", "regression", "benchmark",
    }


def test_validate_coverage_threshold_accepts_bounds():
    assert validate_coverage_threshold(0) == 0.0
    assert validate_coverage_threshold(100) == 100.0
    assert validate_coverage_threshold(85.5) == 85.5


def test_validate_coverage_threshold_rejects_out_of_range():
    for bad in (-1, 100.1, 250, -0.001):
        try:
            validate_coverage_threshold(bad)
        except QualityMatrixError:
            pass
        else:
            raise AssertionError(f"expected QualityMatrixError for {bad!r}")


def test_validate_coverage_threshold_rejects_non_numeric_and_bool():
    for bad in ("85", None, [], {}, True, False):
        try:
            validate_coverage_threshold(bad)
        except QualityMatrixError:
            pass
        else:
            raise AssertionError(f"expected QualityMatrixError for {bad!r}")


def test_missing_receipt_fails_closed(tmp_path):
    verdict = evaluate_quality_matrix(str(tmp_path))
    assert verdict["ready"] is False
    assert verdict["reason_code"] == "quality_matrix_missing"


def test_passing_receipt_is_ready(tmp_path):
    (tmp_path / "quality-matrix.json").write_text(json.dumps(_passing_receipt()), encoding="utf-8")
    verdict = evaluate_quality_matrix(str(tmp_path))
    assert verdict["ready"] is True
    assert verdict["reason_code"] == "quality_matrix_verified"
    assert verdict["coverage_threshold"] == 85.0
    assert verdict["coverage_measured"] == 90.0


def test_each_missing_requirement_blocks_individually(tmp_path):
    for name in REQUIRED_REQUIREMENTS:
        receipt = _passing_receipt()
        del receipt["requirements"][name]
        (tmp_path / "quality-matrix.json").write_text(json.dumps(receipt), encoding="utf-8")
        verdict = evaluate_quality_matrix(str(tmp_path))
        assert verdict["ready"] is False, name
        assert verdict["reason_code"] == f"quality_{name}_missing", name


def test_each_failing_requirement_blocks_individually(tmp_path):
    for name in REQUIRED_REQUIREMENTS:
        receipt = _passing_receipt()
        receipt["requirements"][name] = {"status": "fail", "proof_ref": "x"}
        (tmp_path / "quality-matrix.json").write_text(json.dumps(receipt), encoding="utf-8")
        verdict = evaluate_quality_matrix(str(tmp_path))
        assert verdict["ready"] is False, name
        assert verdict["reason_code"] == f"quality_{name}_failed", name


def test_requirement_without_proof_ref_is_unproven(tmp_path):
    receipt = _passing_receipt()
    receipt["requirements"]["unit"] = {"status": "pass", "proof_ref": ""}
    (tmp_path / "quality-matrix.json").write_text(json.dumps(receipt), encoding="utf-8")
    verdict = evaluate_quality_matrix(str(tmp_path))
    assert verdict["ready"] is False
    assert verdict["reason_code"] == "quality_unit_unproven"


def test_coverage_below_threshold_blocks(tmp_path):
    receipt = _passing_receipt(coverage={"measured": 84.9})
    (tmp_path / "quality-matrix.json").write_text(json.dumps(receipt), encoding="utf-8")
    verdict = evaluate_quality_matrix(str(tmp_path))
    assert verdict["ready"] is False
    assert verdict["reason_code"] == "coverage_below_threshold"
    assert verdict["coverage_measured"] == 84.9


def test_coverage_exactly_at_threshold_passes(tmp_path):
    receipt = _passing_receipt(coverage={"measured": 85.0})
    (tmp_path / "quality-matrix.json").write_text(json.dumps(receipt), encoding="utf-8")
    verdict = evaluate_quality_matrix(str(tmp_path))
    assert verdict["ready"] is True


def test_coverage_missing_or_non_numeric_blocks(tmp_path):
    for bad in ({"measured": None}, {}, {"measured": "90"}, {"measured": True}):
        receipt = _passing_receipt(coverage=bad)
        (tmp_path / "quality-matrix.json").write_text(json.dumps(receipt), encoding="utf-8")
        verdict = evaluate_quality_matrix(str(tmp_path))
        assert verdict["ready"] is False, bad
        assert verdict["reason_code"] == "coverage_unmeasured", bad


def test_custom_coverage_threshold_is_honored(tmp_path):
    receipt = _passing_receipt(coverage_threshold=95, coverage={"measured": 90.0})
    (tmp_path / "quality-matrix.json").write_text(json.dumps(receipt), encoding="utf-8")
    verdict = evaluate_quality_matrix(str(tmp_path))
    assert verdict["ready"] is False
    assert verdict["reason_code"] == "coverage_below_threshold"
    assert verdict["coverage_threshold"] == 95.0


def test_invalid_coverage_threshold_config_fails_closed_not_default(tmp_path):
    receipt = _passing_receipt(coverage_threshold=150)
    (tmp_path / "quality-matrix.json").write_text(json.dumps(receipt), encoding="utf-8")
    verdict = evaluate_quality_matrix(str(tmp_path))
    assert verdict["ready"] is False
    assert verdict["reason_code"] == "coverage_threshold_invalid"


def test_build_quality_matrix_template_is_all_unset_never_a_passing_default():
    template = build_quality_matrix_template()
    for name in REQUIRED_REQUIREMENTS:
        assert template["requirements"][name]["status"] == "unset"
    assert template["coverage"]["measured"] is None


if __name__ == "__main__":
    sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_quality_matrix_unit")
