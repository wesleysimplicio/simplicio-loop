from __future__ import annotations

import asyncio

from bench.benchmark_local_first_892 import benchmark


def test_benchmark_receipt_declares_unmeasured_phases_and_resources() -> None:
    receipt = asyncio.run(benchmark(repetitions=3, physical_cap=1, delay_seconds=0.0001))
    phases = receipt["methodology"]["phases"]
    assert phases["cold"]["value"] is None
    assert phases["warm"]["null_reason"]
    resources = receipt["resource_metrics"]
    assert resources["peak_rss_bytes"] is None
    assert resources["null_reasons"]["tokens"] == "offline model-free benchmark"
    assert "source_commit" in receipt["environment"]
