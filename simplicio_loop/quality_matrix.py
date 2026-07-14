"""Quality matrix gate (#278): fail-closed evidence for every mandatory quality lane.

No issue/delivery can be reported ``done`` unless a versioned quality-matrix receipt
proves implementation, unit, integration, system, regression and benchmark evidence
*and* a measured coverage percentage at/above the configured minimum (default 85%).
The matrix is intentionally data-only: it reads a JSON receipt produced by the run
(``quality-matrix.json``) and renders a structured, fail-closed verdict — it never
invents a passing result for a requirement that is missing or unmeasured.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

SCHEMA = "simplicio.quality-matrix/v1"

# Every one of these lanes is mandatory (#278 acceptance criteria): implementation,
# unit, integration, system and regression evidence, plus a performance benchmark.
# Coverage is validated separately since it carries a numeric threshold rather than
# a simple pass/fail state.
REQUIRED_REQUIREMENTS: Tuple[str, ...] = (
    "implementation",
    "unit",
    "integration",
    "system",
    "regression",
    "benchmark",
)

DEFAULT_COVERAGE_THRESHOLD = 85.0
RECEIPT_FILENAME = "quality-matrix.json"


class QualityMatrixError(ValueError):
    """Raised when the quality-matrix policy itself is malformed."""


def _gate(name: str, ok: bool, reason_code: str, detail: str) -> Dict[str, Any]:
    return {"name": name, "status": "pass" if ok else "fail", "reason_code": reason_code, "detail": detail}


def receipt_path(run_dir: str) -> Path:
    return Path(run_dir) / RECEIPT_FILENAME


def _load_json(path: Path) -> Dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def validate_coverage_threshold(value: Any) -> float:
    """Validate a configured coverage threshold, fail-closed on anything malformed.

    Raises :class:`QualityMatrixError` rather than silently clamping — an invalid
    policy must never be quietly downgraded to a permissive default.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QualityMatrixError(f"coverage threshold must be numeric, got {value!r}")
    threshold = float(value)
    if threshold < 0 or threshold > 100:
        raise QualityMatrixError(f"coverage threshold must be between 0 and 100, got {threshold!r}")
    return threshold


def _requirement_gate(name: str, requirements: Dict[str, Any]) -> Dict[str, Any]:
    entry = requirements.get(name)
    if not isinstance(entry, dict):
        return _gate(name, False, f"quality_{name}_missing", f"required '{name}' evidence is missing from the quality matrix")
    status = str(entry.get("status") or "").strip().lower()
    proof_ref = str(entry.get("proof_ref") or "").strip()
    if status != "pass":
        return _gate(name, False, f"quality_{name}_failed",
                     f"required '{name}' evidence is not passing (status={status or 'unset'!r})")
    if not proof_ref:
        return _gate(name, False, f"quality_{name}_unproven",
                     f"required '{name}' evidence has no proof reference")
    return _gate(name, True, f"quality_{name}_verified", f"'{name}' evidence verified via {proof_ref}")


def evaluate_quality_matrix(run_dir: str) -> Dict[str, Any]:
    """Evaluate the fail-closed quality gate for one run directory.

    Returns a dict with ``ready`` (bool), ``reason_code``/``reason`` for the first
    failing gate (or the success verdict), the full ``gates`` list, and the
    coverage figures actually measured vs. required — every field the delivery
    gate and the CLI need to explain a block.
    """
    gates: List[Dict[str, Any]] = []
    result: Dict[str, Any] = {
        "schema": SCHEMA,
        "ready": False,
        "reason_code": "quality_matrix_incomplete",
        "reason": "quality matrix gates not satisfied",
        "coverage_threshold": DEFAULT_COVERAGE_THRESHOLD,
        "coverage_measured": None,
        "gates": gates,
    }

    path = receipt_path(run_dir)
    receipt = _load_json(path)
    if not receipt:
        gate = _gate("quality_matrix", False, "quality_matrix_missing", f"{RECEIPT_FILENAME} is missing or unreadable")
        gates.append(gate)
        result["reason_code"] = gate["reason_code"]
        result["reason"] = gate["detail"]
        return result
    gates.append(_gate("quality_matrix", True, "quality_matrix_present", f"{RECEIPT_FILENAME} loaded"))

    raw_threshold = receipt.get("coverage_threshold", DEFAULT_COVERAGE_THRESHOLD)
    try:
        threshold = validate_coverage_threshold(raw_threshold)
    except QualityMatrixError as exc:
        gate = _gate("coverage_threshold", False, "coverage_threshold_invalid", str(exc))
        gates.append(gate)
        result["reason_code"] = gate["reason_code"]
        result["reason"] = gate["detail"]
        return result
    result["coverage_threshold"] = threshold
    gates.append(_gate("coverage_threshold", True, "coverage_threshold_valid",
                       f"coverage threshold {threshold}% is within [0, 100]"))

    requirements = receipt.get("requirements")
    if not isinstance(requirements, dict):
        requirements = {}

    for name in REQUIRED_REQUIREMENTS:
        gate = _requirement_gate(name, requirements)
        gates.append(gate)
        if gate["status"] != "pass":
            result["reason_code"] = gate["reason_code"]
            result["reason"] = gate["detail"]
            return result

    coverage = receipt.get("coverage")
    measured = (coverage or {}).get("measured") if isinstance(coverage, dict) else None
    if isinstance(measured, bool) or not isinstance(measured, (int, float)):
        gate = _gate("coverage", False, "coverage_unmeasured", "coverage.measured is missing or not numeric")
        gates.append(gate)
        result["reason_code"] = gate["reason_code"]
        result["reason"] = gate["detail"]
        return result
    measured = float(measured)
    result["coverage_measured"] = measured
    if measured < threshold:
        gate = _gate("coverage", False, "coverage_below_threshold",
                     f"measured coverage {measured}% is below the required {threshold}%")
        gates.append(gate)
        result["reason_code"] = gate["reason_code"]
        result["reason"] = gate["detail"]
        return result
    gates.append(_gate("coverage", True, "coverage_sufficient",
                       f"measured coverage {measured}% meets the required {threshold}%"))

    result.update({
        "ready": True,
        "reason_code": "quality_matrix_verified",
        "reason": "implementation, unit, integration, system, regression, benchmark and coverage gates all pass",
    })
    return result


def build_quality_matrix_template(coverage_threshold: float = DEFAULT_COVERAGE_THRESHOLD) -> Dict[str, Any]:
    """Return an all-failing template receipt — a starting point, never a passing default."""
    validate_coverage_threshold(coverage_threshold)
    return {
        "schema": SCHEMA,
        "coverage_threshold": coverage_threshold,
        "requirements": {
            name: {"status": "unset", "proof_ref": "", "detail": ""}
            for name in REQUIRED_REQUIREMENTS
        },
        "coverage": {"measured": None},
    }


__all__ = [
    "SCHEMA",
    "RECEIPT_FILENAME",
    "REQUIRED_REQUIREMENTS",
    "DEFAULT_COVERAGE_THRESHOLD",
    "QualityMatrixError",
    "receipt_path",
    "validate_coverage_threshold",
    "evaluate_quality_matrix",
    "build_quality_matrix_template",
]
