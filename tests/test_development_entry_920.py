from __future__ import annotations

import pytest

from simplicio_loop.development_entry import DevelopmentAssessment, route_development


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
