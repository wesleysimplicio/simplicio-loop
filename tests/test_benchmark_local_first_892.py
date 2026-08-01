from __future__ import annotations

import asyncio

from bench.benchmark_local_first_892 import benchmark


def test_benchmark_receipt_declares_unmeasured_phases_and_resources() -> None:
    receipt = asyncio.run(benchmark(repetitions=10, warmups=1, physical_cap=1, delay_seconds=0.0001))
    assert receipt["methodology"]["repetitions"] == 10
    assert receipt["methodology"]["warmups"] == 1
    phases = receipt["methodology"]["phases"]
    assert phases["cold"]["value"] is None
    assert phases["warm"]["null_reason"]
    resources = receipt["resource_metrics"]
    assert resources["peak_rss_bytes"] is None
    assert resources["null_reasons"]["tokens"] == "offline model-free benchmark"
    assert "source_commit" in receipt["environment"]


def test_benchmark_rejects_fewer_than_ten_measured_repetitions() -> None:
    try:
        asyncio.run(benchmark(repetitions=3, warmups=0))
    except ValueError as exc:
        assert "repetitions must be >=10" in str(exc)
    else:
        raise AssertionError("benchmark accepted fewer than ten repetitions")
