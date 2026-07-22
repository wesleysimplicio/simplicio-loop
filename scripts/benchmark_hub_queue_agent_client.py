#!/usr/bin/env python3
"""Reproducible fake-transport throughput benchmark for HubQueueAgentClient (#615)."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simplicio_loop.stage_agent_coordinator import HubQueueAgentClient


class MemoryHub:
    def request(self, request_id, method, **payload):
        if method == "ping":
            return {"ok": True, "started": True}
        return {"ok": True, "job": {"state": "running"}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    samples = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        hub = MemoryHub()
        client = HubQueueAgentClient(
            command=[sys.executable, "-c", "pass"], client_factory=lambda: hub,
            journal_path=root / "journal.jsonl", base_tmp_dir=root / "runs", cwd=Path.cwd(),
        )
        for index in range(args.iterations):
            started = time.perf_counter_ns()
            client.claim(
                role="review_panel", stage="validating",
                context={"run_id": "bench", "task_id": "bench", "attempt_id": str(index),
                         "fence": "fence", "plan_revision": 1},
            )
            samples.append((time.perf_counter_ns() - started) / 1_000_000)
    ordered = sorted(samples)
    result = {
        "schema": "simplicio.hub-queue-agent-benchmark/v1",
        "iterations": args.iterations,
        "operation": "claim with durable before/after fsync journal and fake Hub transport",
        "mean_ms": statistics.fmean(samples),
        "median_ms": statistics.median(samples),
        "p95_ms": ordered[max(0, int(len(ordered) * 0.95) - 1)],
        "throughput_ops_per_second": 1000 / statistics.fmean(samples),
        "raw_samples_ms": samples,
    }
    payload = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
