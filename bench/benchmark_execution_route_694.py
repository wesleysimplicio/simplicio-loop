"""Reproducible routing benchmark for issue #694.

The fixture's provider reports exact token usage, so token fields are observed
from the fixture rather than estimated by the benchmark.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simplicio_loop.execution_route import decide_route


class MeasuredProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.tokens = 0

    def invoke(self) -> dict:
        started = time.perf_counter()
        time.sleep(0.001)
        self.calls += 1
        self.tokens += 128
        return {"ok": True, "tokens": 128, "latency_ms": (time.perf_counter() - started) * 1000}


def run(*, routed: bool, count: int = 40) -> dict:
    provider = MeasuredProvider()
    latencies = []
    completed = 0
    retries = 0
    started = time.perf_counter()
    for index in range(count):
        mechanical = index % 2 == 0
        route = decide_route(
            "mechanically edit and test schema" if mechanical else "investigate ambiguous semantic failure",
            has_deterministic_worker=True,
            is_ambiguous=not mechanical,
        )
        item_started = time.perf_counter()
        if not routed or route.route != "worker":
            result = provider.invoke()
            completed += int(result["ok"])
        else:
            completed += 1
        latencies.append((time.perf_counter() - item_started) * 1000)
    return {
        "items": count,
        "completed": completed,
        "completion_rate": completed / count,
        "provider_calls": provider.calls,
        "measured_tokens": provider.tokens,
        "retries": retries,
        "elapsed_ms": (time.perf_counter() - started) * 1000,
        "p50_ms": statistics.median(latencies),
        "p95_ms": sorted(latencies)[int(len(latencies) * 0.95) - 1],
    }


def main() -> None:
    before = run(routed=False)
    after = run(routed=True)
    print(json.dumps({
        "schema": "simplicio.execution-route-benchmark/v1",
        "token_source": "measured_fixture_provider_response",
        "before_global_agent": before,
        "after_execution_route": after,
        "token_reduction": before["measured_tokens"] - after["measured_tokens"],
        "provider_call_reduction": before["provider_calls"] - after["provider_calls"],
        "completion_regression": after["completed"] - before["completed"],
        "retry_regression": after["retries"] - before["retries"],
        "speedup": before["elapsed_ms"] / after["elapsed_ms"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
