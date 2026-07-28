#!/usr/bin/env python3
"""Loop stage -> Runtime authority -> fake LiteRT -> receipt E2E."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from simplicio_loop.device_fabric import (  # noqa: E402
    DeviceFabric, DeviceRequest, DeviceRequirement, DeviceStageAdapter,
    FakeRuntimeDeviceAuthority, write_evidence,
)


async def scenario(evidence_dir):
    runtime = FakeRuntimeDeviceAuthority({
        "CPU": {
            "slots": 1, "memory_bytes": 512,
            "capabilities": ["completion", "embedding"],
            "backend": "fake-litert-cpu",
        }
    })
    host_a = DeviceFabric(runtime, queue_capacity=4, max_in_flight=2)
    host_b = DeviceFabric(runtime, queue_capacity=4, max_in_flight=2)
    stage_host_a = DeviceStageAdapter(host_a)
    stage_host_b = DeviceStageAdapter(host_b)

    async def stage(cancel):
        await asyncio.sleep(0.003)
        return {"stage": "validating", "verdict": "pass"}

    requirement = DeviceRequirement(
        "completion", ("NPU", "CPU"), ("CPU",),
        latency_class="interactive", memory_class="small",
        memory_bytes=128, deadline_seconds=2,
    )
    first = stage_host_a.run(
        stage_id="validating", stage_instance_id="validating@host-a",
        request=DeviceRequest(
            "e2e-host-a", "session-a", "host-a", "e2e:host-a", requirement
        ),
        operation=stage,
    )
    second = stage_host_b.run(
        stage_id="validating", stage_instance_id="validating@host-b",
        request=DeviceRequest(
            "e2e-host-b", "session-b", "host-b", "e2e:host-b", requirement
        ),
        operation=stage,
    )
    receipts = await asyncio.gather(first, second)
    paths = [
        str(write_evidence(evidence_dir, receipt["device_receipt"]))
        for receipt in receipts
    ]
    await asyncio.gather(host_a.close(), host_b.close())
    snapshot = await runtime.capacity_snapshot()
    return {
        "schema": "simplicio.device-fabric-e2e/v1",
        "classification": "MEASURED_LOCAL",
        "receipts": receipts,
        "evidence_paths": paths,
        "physical_max_used": runtime.max_used,
        "physical_slots_available_after": snapshot["devices"]["CPU"]["slots_available"],
        "runtime_execution_calls": runtime.execution_calls,
        "local_llm": False,
        "model_provider_started": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    args = parser.parse_args()
    payload = asyncio.run(scenario(args.evidence_dir))
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["receipt_hash"] = "sha256:" + hashlib.sha256(raw).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "status": "passed",
        "receipts": len(payload["receipts"]),
        "physical_max_used": payload["physical_max_used"],
        "receipt_hash": payload["receipt_hash"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
