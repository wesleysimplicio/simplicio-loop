from scripts.benchmark_map_service import benchmark


def test_benchmark_receipt_compares_naive_and_centralized_work(tmp_path):
    receipt = benchmark(3, 2)
    assert receipt["naive_builds"] == 6
    assert receipt["centralized_equivalent_builds"] == 3
    assert receipt["fallback_verified"] is True
