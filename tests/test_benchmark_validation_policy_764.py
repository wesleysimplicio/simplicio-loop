from scripts.benchmark_validation_policy_764 import run_case


def test_policy_benchmark_reports_bounded_selection_and_scope():
    result = run_case(100, 2)
    assert result["adaptive_selected"] < result["full_selected"]
    assert result["measurement_scope"] == "policy_selection_only"
    assert result["local_llm_started"] is False
