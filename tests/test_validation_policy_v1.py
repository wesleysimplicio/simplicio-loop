from simplicio_loop.validation_policy import (
    VALIDATION_RECEIPT_SCHEMA_V1,
    ValidationCandidate,
    ValidationInputs,
    ValidationPolicy,
)


def candidates():
    return (
        ValidationCandidate("unit-fast", "focused", 20),
        ValidationCandidate("syntax", "static", 5),
        ValidationCandidate("integration", "impacted", 500),
        ValidationCandidate("full-suite", "full", 5_000),
    )


def test_profiles_are_deterministic_and_explainable():
    policy = ValidationPolicy()
    receipt = policy.decide(ValidationInputs(phase="edit", candidates=candidates()))
    assert receipt.schema == VALIDATION_RECEIPT_SCHEMA_V1
    assert receipt.profile == "edit"
    assert receipt.selected_tests == ("syntax", "unit-fast")
    assert receipt.reason_codes == ()
    assert receipt.cache_allowed is True
    assert receipt.explain() == receipt.explain()


def test_stale_or_unknown_impact_escalates_conservatively():
    receipt = ValidationPolicy().decide(
        ValidationInputs(
            phase="edit", map_fresh=False, impact_known=False, candidates=candidates()
        )
    )
    assert receipt.profile == "converge"
    assert receipt.final_gate_required is True
    assert receipt.selected_tests == (
        "syntax",
        "unit-fast",
        "integration",
        "full-suite",
    )
    assert {"MAP_STALE", "IMPACT_UNKNOWN", "CONSERVATIVE_ESCALATION"}.issubset(
        receipt.reason_codes
    )
    assert receipt.cache_allowed is False


def test_pre_promote_never_downgrades_and_requires_final_gate():
    receipt = ValidationPolicy().decide(
        ValidationInputs(phase="pre_promote", candidates=candidates())
    )
    assert receipt.profile == "pre_promote"
    assert receipt.final_gate_required is True
    assert receipt.selected_tests == (
        "syntax",
        "unit-fast",
        "integration",
        "full-suite",
    )
    assert "FINAL_GATE_REQUIRED" in receipt.reason_codes


def test_repeated_failure_adds_full_validation_in_converge():
    receipt = ValidationPolicy().decide(
        ValidationInputs(phase="converge", previous_failures=2, candidates=candidates())
    )
    assert receipt.profile == "converge"
    assert receipt.selected_tests == (
        "syntax",
        "unit-fast",
        "integration",
        "full-suite",
    )
    assert "REPEATED_FAILURE" in receipt.reason_codes


def test_cache_key_is_order_independent_but_changes_with_inputs():
    policy = ValidationPolicy()
    first = policy.decide(ValidationInputs(phase="edit", candidates=candidates()))
    second = policy.decide(
        ValidationInputs(phase="edit", candidates=tuple(reversed(candidates())))
    )
    changed = policy.decide(
        ValidationInputs(phase="edit", previous_failures=1, candidates=candidates())
    )
    assert first.cache_key == second.cache_key
    assert first.cache_key != changed.cache_key
    assert len(first.cache_key) == 64
