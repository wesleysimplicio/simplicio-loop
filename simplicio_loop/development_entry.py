"""Deterministic development execution-mode assessment for Loop entrypoints."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

SCHEMA = "simplicio.loop-development-routing/v1"
GATE_SCHEMA = "simplicio.loop-development-route-gate/v1"
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

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DevelopmentAssessment":
        if not isinstance(payload, dict) or not str(payload.get("task_id") or "").strip():
            raise DevelopmentRouteError("assessment requires task_id")
        expected = cls(
            task_id=str(payload["task_id"]), complexity=str(payload.get("complexity", "simple")),
            independent_ready=int(payload.get("independent_ready", 0)),
            critical=bool(payload.get("critical", False)), heavy=bool(payload.get("heavy", False)),
            uncertain=bool(payload.get("uncertain", False)), version=str(payload.get("version", "1")),
        )
        if payload.get("assessment_hash") != expected.to_dict()["assessment_hash"]:
            raise DevelopmentRouteError("assessment hash is invalid")
        return expected


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

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DevelopmentRoute":
        if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
            raise DevelopmentRouteError("unsupported development route schema")
        fields = ("task_id", "requested_mode", "selected_mode", "reason_code",
                  "assessment_hash", "max_iterations")
        if any(field not in payload for field in fields):
            raise DevelopmentRouteError("development route is incomplete")
        route = cls(
            task_id=str(payload["task_id"]), requested_mode=str(payload["requested_mode"]),
            selected_mode=str(payload["selected_mode"]), reason_code=str(payload["reason_code"]),
            assessment_hash=str(payload["assessment_hash"]), max_iterations=int(payload["max_iterations"]),
        )
        if payload.get("receipt_hash") != route.to_dict()["receipt_hash"]:
            raise DevelopmentRouteError("route receipt hash is invalid")
        return route


class DevelopmentRouteError(ValueError):
    """A route cannot be admitted for the current assessment."""


def validate_route(route: DevelopmentRoute, assessment: DevelopmentAssessment) -> dict[str, Any]:
    """Revalidate a persisted route before dispatch or promotion."""
    if route.task_id != assessment.task_id:
        raise DevelopmentRouteError("route task does not match assessment")
    assessment_hash = assessment.to_dict()["assessment_hash"]
    if route.assessment_hash != assessment_hash:
        raise DevelopmentRouteError("route assessment is stale")
    expected = route_development(assessment, route.requested_mode, max_iterations=route.max_iterations)
    if (route.selected_mode, route.reason_code, route.max_iterations) != (
        expected.selected_mode, expected.reason_code, expected.max_iterations
    ):
        raise DevelopmentRouteError("route decision does not match assessment policy")
    payload = {"schema": GATE_SCHEMA, "task_id": route.task_id,
               "assessment_hash": assessment_hash, "route_hash": route.to_dict()["receipt_hash"],
               "selected_mode": route.selected_mode, "status": "ADMITTED",
               "reason_code": "ROUTE_FRESH_AND_POLICY_VALID"}
    payload["receipt_hash"] = _hash(payload)
    return payload


def admit_route_payload(route_payload: dict[str, Any], assessment_payload: dict[str, Any]) -> dict[str, Any]:
    """Rehydrate and admit a persisted route without trusting caller fields."""
    return validate_route(DevelopmentRoute.from_dict(route_payload),
                          DevelopmentAssessment.from_dict(assessment_payload))


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


__all__ = ["GATE_SCHEMA", "MODES", "SCHEMA", "DevelopmentAssessment", "DevelopmentRoute",
           "DevelopmentRouteError", "admit_route_payload", "route_development", "validate_route"]
