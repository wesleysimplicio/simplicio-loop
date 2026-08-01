"""Deterministic development execution-mode assessment for Loop entrypoints."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

SCHEMA = "simplicio.loop-development-routing/v1"
MODES = ("auto", "one_shot", "converge", "parallel_drain", "heavy_compute", "critical_serial")


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class DevelopmentAssessment:
    task_id: str
    complexity: str = "simple"
    independent_ready: int = 0
    critical: bool = False
    heavy: bool = False
    uncertain: bool = False
    version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        payload = {"task_id": self.task_id, "complexity": self.complexity,
                   "independent_ready": self.independent_ready, "critical": self.critical,
                   "heavy": self.heavy, "uncertain": self.uncertain, "version": self.version}
        payload["assessment_hash"] = _hash(payload)
        return payload


@dataclass(frozen=True)
class DevelopmentRoute:
    task_id: str
    requested_mode: str
    selected_mode: str
    reason_code: str
    assessment_hash: str
    max_iterations: int
    schema: str = SCHEMA

    def to_dict(self) -> dict[str, Any]:
        payload = {"schema": self.schema, "task_id": self.task_id,
                   "requested_mode": self.requested_mode, "selected_mode": self.selected_mode,
                   "reason_code": self.reason_code, "assessment_hash": self.assessment_hash,
                   "max_iterations": self.max_iterations}
        payload["receipt_hash"] = _hash(payload)
        return payload


def route_development(assessment: DevelopmentAssessment, mode: str = "auto", *, max_iterations: int = 1) -> DevelopmentRoute:
    requested = str(mode or "auto").lower()
    if requested not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if max_iterations < 1:
        raise ValueError("max_iterations must be positive")
    if assessment.critical and requested not in {"auto", "critical_serial"}:
        raise ValueError("critical policy requires critical_serial")
    if requested == "auto":
        if assessment.critical:
            selected, reason, iterations = "critical_serial", "critical_policy", 1
        elif assessment.heavy:
            selected, reason, iterations = "heavy_compute", "heavy_assessment", max(2, max_iterations)
        elif assessment.independent_ready >= 2:
            selected, reason, iterations = "parallel_drain", "ready_set_parallel", max(1, max_iterations)
        elif assessment.uncertain or assessment.complexity != "simple":
            selected, reason, iterations = "converge", "complex_or_uncertain", max(2, max_iterations)
        else:
            selected, reason, iterations = "one_shot", "simple_assessment", 1
    else:
        selected, reason, iterations = requested, "explicit_mode", max_iterations
        if selected == "one_shot":
            iterations = 1
    return DevelopmentRoute(assessment.task_id, requested, selected, reason,
                            assessment.to_dict()["assessment_hash"], iterations)


__all__ = ["MODES", "SCHEMA", "DevelopmentAssessment", "DevelopmentRoute", "route_development"]
