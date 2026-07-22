#!/usr/bin/env python3
"""Reproducible real-socket/process benchmark for HubQueueAgentClient (#615)."""
from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simplicio_loop.hub_daemon import HubDaemon, HubSocketClient, HubSocketServer, default_endpoint
from simplicio_loop.hub_queue_agent import HubQueueAgentClient


def _summary(samples: list[float]) -> dict[str, object]:
    ordered = sorted(samples)
    return {
        "mean_ms": statistics.fmean(samples),
        "median_ms": statistics.median(samples),
        "p95_ms": ordered[max(0, int(len(ordered) * 0.95) - 1)],
        "raw_samples_ms": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")

    phases = {name: [] for name in ("prepare_claim", "send_to_terminal", "collect", "total")}
    started_all = time.perf_counter()
    with tempfile.TemporaryDirectory() as directory:
        endpoint = default_endpoint(directory)
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        server = HubSocketServer(daemon, endpoint, "unix")
        server.start()
        try:
            client = HubQueueAgentClient(HubSocketClient(endpoint, transport="unix"), strict=True)
            for index in range(args.iterations):
                total_started = time.perf_counter_ns()
                spec = {
                    "schema": "simplicio.process-spec/v1", "argv": [sys.executable, "-c", "pass"],
                    "cwd": str(Path.cwd()), "cwd_allowlist": [str(Path.cwd())], "env": {},
                    "env_allowlist": [], "timeout_seconds": 10, "max_output_bytes": 4096,
                    "priority": 100, "idempotency_key": f"bench-process-{index}", "shell": False,
                }
                context = {
                    "run_id": "bench-615", "task_id": "bench-615", "attempt_id": str(index),
                    "fence": "source-fence", "process_spec": spec,
                    "resources": {"cpu": 0, "memory_bytes": 0, "disk_bytes": 0, "gpu": 0, "processes": 0, "connections": 0, "tokens": 0},
                }
                phase_started = time.perf_counter_ns()
                handle = client.claim(role="review_panel", stage="validating", context=context)
                phases["prepare_claim"].append((time.perf_counter_ns() - phase_started) / 1_000_000)
                phase_started = time.perf_counter_ns()
                client.send(handle, context)
                deadline = time.monotonic() + 10
                status = client.status(handle)
                while status["status"] not in {"passed", "failed", "cancelled", "timed_out"}:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("benchmark process did not reach terminal state")
                    time.sleep(0.001)
                    status = client.status(handle)
                phases["send_to_terminal"].append((time.perf_counter_ns() - phase_started) / 1_000_000)
                phase_started = time.perf_counter_ns()
                result = client.collect(handle)
                phases["collect"].append((time.perf_counter_ns() - phase_started) / 1_000_000)
                if result.get("process_result", {}).get("returncode") != 0:
                    raise RuntimeError("benchmark process failed")
                phases["total"].append((time.perf_counter_ns() - total_started) / 1_000_000)
        finally:
            server.shutdown()
            daemon.stop()

    elapsed = time.perf_counter() - started_all
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if os.name == "posix" else None
    result = {
        "schema": "simplicio.hub-queue-agent-benchmark/v2",
        "iterations": args.iterations,
        "transport": "real-unix-socket",
        "process": "real-python-process",
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "phases": {name: _summary(values) for name, values in phases.items()},
        "throughput_ops_per_second": args.iterations / elapsed,
        "peak_rss_kib": rss,
        "peak_rss_reason": None if rss is not None else "unmeasured:on this platform resource.ru_maxrss is unavailable",
    }
    payload = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
