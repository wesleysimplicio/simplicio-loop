#!/usr/bin/env python3
"""Measured LiteRT device-fabric load benchmark for issue #794."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simplicio_loop.device_fabric import (  # noqa: E402
    DeviceFabric, DeviceRequest, DeviceRequirement, FakeRuntimeDeviceAuthority,
)


DEVICES = {
    "CPU": {"slots": 2, "memory_bytes": 2048, "capabilities": ["completion"], "backend": "fake-litert-cpu"},
    "GPU": {"slots": 1, "memory_bytes": 2048, "capabilities": ["completion"], "backend": "fake-litert-gpu"},
    "NPU": {"slots": 1, "memory_bytes": 1024, "capabilities": ["completion"], "backend": "fake-litert-npu"},
}


async def execute_load(count):
    runtime = FakeRuntimeDeviceAuthority(DEVICES)
    fabric = DeviceFabric(runtime, queue_capacity=64, max_in_flight=6)
    order = []

    def operation(request_id):
        async def execute(cancel):
            order.append(request_id)
            await asyncio.sleep(0.001)
            return {"request_id": request_id}
        return execute

    started = time.perf_counter_ns()
    futures = []
    for number in range(count):
        request_id = f"request-{number:03d}"
        request = DeviceRequest(
            request_id, f"session-{number % 4}", f"host-{number % 2}",
            f"run-794:{number}",
            DeviceRequirement(
                "completion", ("NPU", "GPU", "CPU"), ("GPU", "CPU"),
                memory_bytes=64, deadline_seconds=5,
            ),
        )
        futures.append(fabric.submit(request, operation(request_id)))
    receipts = await asyncio.gather(*futures)
    duration = time.perf_counter_ns() - started
    status = fabric.status()
    await fabric.close()
    return {
        "tasks": count,
        "duration_ns": duration,
        "queue_p50_ns": int(statistics.median(
            receipt["queue_time_ns"] for receipt in receipts
        )),
        "execution_p50_ns": int(statistics.median(
            receipt["execution_time_ns"] for receipt in receipts
        )),
        "completed": sum(receipt["status"] == "succeeded" for receipt in receipts),
        "lost": count - len(receipts),
        "max_logical_in_flight": status["metrics"]["max_in_flight"],
        "max_physical": runtime.max_used,
        "fallbacks": status["metrics"]["fallbacks"],
        "fairness_first_window_sessions": len({
            int(item.split("-")[1]) % 4 for item in order[:min(8, len(order))]
        }),
        "model_provider_started": False,
    }


async def benchmark(repeats):
    rows = []
    for count in (1, 6, 64):
        samples = [await execute_load(count) for _ in range(repeats)]
        rows.append({
            **samples[-1],
            "repeats": repeats,
            "duration_median_ns": int(statistics.median(
                sample["duration_ns"] for sample in samples
            )),
            "queue_p50_median_ns": int(statistics.median(
                sample["queue_p50_ns"] for sample in samples
            )),
        })
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = {
        "schema": "simplicio.device-fabric-benchmark/v1",
        "classification": "MEASURED_LOCAL",
        "local_llm": False,
        "physical_capacity": {"CPU": 2, "GPU": 1, "NPU": 1},
        "rows": asyncio.run(benchmark(args.repeats)),
        "cpu_seconds": None,
        "rss_peak_bytes": None,
        "null_reason": "portable per-coroutine CPU and RSS attribution unavailable",
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["receipt_hash"] = "sha256:" + hashlib.sha256(raw).hexdigest()
    encoded = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
