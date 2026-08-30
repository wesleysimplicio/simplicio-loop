"""Measure real submit/claim/complete throughput, p95 latency and RSS for the #504 evidence
receipt (HubRetryQueue, simplicio_loop/hub_queue_retry.py)."""

from __future__ import annotations

import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simplicio_loop.hub_queue_retry import HubRetryQueue


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "queue.db")
        queue = HubRetryQueue(path)
        n = 200

        submit_latencies = []
        started = time.perf_counter()
        for i in range(n):
            tick = time.perf_counter()
            queue.submit({"i": i}, idempotency_key="bench-%d" % i)
            submit_latencies.append(time.perf_counter() - tick)
        submit_elapsed = time.perf_counter() - started

        claim_latencies = []
        complete_latencies = []
        claim_elapsed = 0.0
        complete_elapsed = 0.0
        for _ in range(n):
            tick = time.perf_counter()
            lease = queue.claim("bench-worker", ttl=30)
            claim_latency = time.perf_counter() - tick
            claim_latencies.append(claim_latency)
            claim_elapsed += claim_latency
            assert lease is not None
            tick = time.perf_counter()
            queue.complete(lease)
            complete_latency = time.perf_counter() - tick
            complete_latencies.append(complete_latency)
            complete_elapsed += complete_latency
        queue.close()

        def _stats(name, samples, elapsed):
            ordered = sorted(samples)
            return {
                "operations": len(ordered),
                "throughput_per_second": round(len(ordered) / elapsed, 3),
                "p50_ms": round(statistics.median(ordered) * 1000, 3),
                "p95_ms": round(ordered[int(len(ordered) * 0.95) - 1] * 1000, 3),
                "elapsed_seconds": round(elapsed, 6),
            }

        payload = {
            "schema": "simplicio.hub-queue-retry-benchmark/v1",
            "platform": os.name,
            "submit": _stats("submit", submit_latencies, submit_elapsed),
            "claim": _stats("claim", claim_latencies, claim_elapsed),
            "complete": _stats("complete", complete_latencies, complete_elapsed),
        }
        try:
            import psutil
            payload["rss_bytes"] = psutil.Process().memory_info().rss
        except ImportError:
            payload["rss_bytes"] = None
        print(json.dumps(payload, sort_keys=True))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
