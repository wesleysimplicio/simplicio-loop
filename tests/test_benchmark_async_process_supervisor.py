from scripts import benchmark_async_process_supervisor


def test_benchmark_reports_real_process_metrics() -> None:
    receipt = benchmark_async_process_supervisor.benchmark(rounds=2, concurrency=2)
    assert receipt["schema"] == "simplicio.async-process-supervisor-benchmark/v1"
    assert receipt["processes"] == 4
    assert receipt["throughput_processes_per_second"] > 0
    assert receipt["batch_p95_seconds"] > 0
    assert receipt["duplicate_outcomes"] == 0


def test_benchmark_rejects_non_positive_inputs() -> None:
    try:
        benchmark_async_process_supervisor.benchmark(0, 1)
    except ValueError:
        pass
    else:
        raise AssertionError("expected positive-input validation")
