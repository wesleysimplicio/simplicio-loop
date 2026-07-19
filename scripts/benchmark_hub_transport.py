"""Measure real local Hub IPC latency/throughput for the #503 evidence receipt."""

from __future__ import annotations

import json
import os
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simplicio_loop.hub_daemon import HubDaemon, HubSocketClient, HubSocketServer, default_endpoint, default_transport


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        daemon = HubDaemon(str(root / "hub.lock"))
        daemon.start()
        endpoint = default_endpoint(directory)
        transport = default_transport()
        server = HubSocketServer(daemon, endpoint, transport)
        server.start()
        try:
            def one(index: int) -> float:
                started = time.perf_counter()
                HubSocketClient(endpoint, transport=transport).request("bench-%d" % index, "ping")
                return time.perf_counter() - started

            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=20) as pool:
                samples = sorted(pool.map(one, range(100)))
            elapsed = time.perf_counter() - started
            payload = {
                "schema": "simplicio.hub-transport-benchmark/v1",
                "platform": os.name,
                "transport": transport,
                "requests": len(samples),
                "throughput_per_second": round(len(samples) / elapsed, 3),
                "p50_ms": round(statistics.median(samples) * 1000, 3),
                "p95_ms": round(samples[int(len(samples) * 0.95) - 1] * 1000, 3),
                "elapsed_seconds": round(elapsed, 6),
            }
            try:
                import psutil
                payload["rss_bytes"] = psutil.Process().memory_info().rss
            except ImportError:
                payload["rss_bytes"] = None
            print(json.dumps(payload, sort_keys=True))
            return 0
        finally:
            server.shutdown()
            daemon.stop()


if __name__ == "__main__":
    raise SystemExit(main())
