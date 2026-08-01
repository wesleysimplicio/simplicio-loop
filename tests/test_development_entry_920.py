from __future__ import annotations

import pytest

from simplicio_loop.development_entry import (
    DevelopmentAssessment, DevelopmentRouteError, route_development, validate_route,
)


def test_auto_routes_simple_parallel_heavy_and_critical_assessments() -> None:
    assert route_development(DevelopmentAssessment("one")).selected_mode == "one_shot"
    assert route_development(DevelopmentAssessment("many", independent_ready=2)).selected_mode == "parallel_drain"
    assert route_development(DevelopmentAssessment("cpu", heavy=True)).selected_mode == "heavy_compute"
    critical = route_development(DevelopmentAssessment("migration", critical=True), max_iterations=5)
    assert critical.selected_mode == "critical_serial"
    assert critical.max_iterations == 1


def test_critical_cannot_be_overridden_and_route_is_hash_bound() -> None:
    assessment = DevelopmentAssessment("critical", critical=True)
    with pytest.raises(ValueError, match="critical_serial"):
        route_development(assessment, "parallel_drain")
    route = route_development(assessment)
    assert route.assessment_hash == assessment.to_dict()["assessment_hash"]
    assert route.to_dict()["receipt_hash"].startswith("sha256:")


def test_route_gate_rejects_stale_assessment_and_admits_fresh_route() -> None:
    assessment = DevelopmentAssessment("task", uncertain=True)
    route = route_development(assessment, max_iterations=3)
    gate = validate_route(route, assessment)
    assert gate["status"] == "ADMITTED"
    assert gate["selected_mode"] == "converge"
    with pytest.raises(DevelopmentRouteError, match="stale"):
        validate_route(route, DevelopmentAssessment("task", critical=True))


def test_route_gate_rejects_tampered_decision() -> None:
    assessment = DevelopmentAssessment("task")
    route = route_development(assessment)
    tampered = route.__class__(route.task_id, route.requested_mode, "converge",
                               route.reason_code, route.assessment_hash, route.max_iterations)
    with pytest.raises(DevelopmentRouteError, match="decision"):
        validate_route(tampered, assessment)
