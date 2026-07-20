from __future__ import annotations

import json

from scripts import benchmark_hub_queue_retry


def test_benchmark_reports_real_submit_claim_complete_throughput_and_p95(capsys) -> None:
    assert benchmark_hub_queue_retry.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["schema"] == "simplicio.hub-queue-retry-benchmark/v1"
    for stage in ("submit", "claim", "complete"):
        stats = payload[stage]
        assert stats["operations"] == 200
        assert stats["throughput_per_second"] > 0
        assert stats["p50_ms"] >= 0
        assert stats["p95_ms"] >= stats["p50_ms"]
    # rss_bytes is either a real measurement (psutil present) or an honest None
    # (psutil absent) — never a fabricated number.
    assert payload["rss_bytes"] is None or payload["rss_bytes"] > 0
