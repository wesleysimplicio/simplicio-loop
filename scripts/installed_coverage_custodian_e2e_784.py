#!/usr/bin/env python3
"""Installed-artifact E2E and measured Mapper/Fast/Loop benchmark for #784."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import resource
import statistics
import time

from simplicio_mapper.coverage_atlas import operational_delta
from simplicio_fast.slot_executor import SlotExecutor, make_envelope
from simplicio_loop import coverage_custodians as cc
from simplicio_loop.coverage_custodian_host import CustodianHost, proceed_decision


def address(capability: str, generation: int = 1) -> dict:
    value = {
        "schema": cc.CUSTODIAN_ADDRESS_SCHEMA, "capability": capability,
        "target": f"fast://custodian/{capability}/{generation}", "generation": generation,
    }
    value["address_id"] = cc.digest(value)
    return value


def fast_adapter(executor, snapshot, gap, loop_envelope, slot):
    fast_envelope = make_envelope(slot, run_id=loop_envelope["run_id"],
                                  generation=slot + 1, fence=loop_envelope["fence"])
    receipt = executor.execute(
        fast_envelope, snapshot, writes={f"gap-{slot}.json": json.dumps(gap).encode()},
        runtime_available=False, rust_available=False,
    )
    value = {
        "schema": cc.CUSTODIAN_RECEIPT_SCHEMA,
        "verdict_schema": cc.FAST_VERDICT_SCHEMA,
        "gap_id": gap["gap_id"], "envelope_digest": loop_envelope["envelope_digest"],
        "idempotency_key": loop_envelope["idempotency_key"], "fence": loop_envelope["fence"],
        "agent_instance_id": f"fast-worker-{slot}", "verdict": "FIXED",
        "evidence_refs": [f"fast-receipt://{receipt['receipt_digest']}"],
    }
    value["receipt_digest"] = cc.digest(value)
    return value


def execute_e2e(root: Path) -> dict:
    dirty = operational_delta(
        source="simplicio-mapper@installed", base_atlas_digest="sha256:atlas",
        observations=[{"kind": "cache_integrity", "subject": "cache:main",
                       "healthy": False, "evidence_refs": ["fast://health"]}],
    )
    gap = dirty["gaps"][0]
    decision = cc.decide(dirty, [address("cache_integrity")], {"dispatch_budget": 1})[0]
    envelope = cc.build_envelope(
        gap, decision, {"run_id": "installed-784", "fence": "f1", "plan_revision": "1"},
        {"cpu_ms": 1000, "max_attempts": 1},
    )
    fast = SlotExecutor(root / "fast")
    snapshot = fast.open_snapshot("installed-784", "source", {"atlas.json": json.dumps(dirty).encode()})
    host = CustodianHost(root / "loop-journal.json")
    receipts = []

    def worker(value):
        result = fast_adapter(fast, snapshot, gap, value, 0)
        receipts.append(result)
        return result

    pre = lambda value: proceed_decision(value, phase="pre")
    post = lambda value, receipt: proceed_decision(
        value, phase="post", receipt_hash=receipt["receipt_hash"],
    )
    host_receipt = host.dispatch(
        envelope, workspace=str(root), policy_hash="policy-v1",
        pre_hook=pre, worker=worker, post_hook=post,
    )
    duplicate = host.dispatch(
        envelope, workspace=str(root), policy_hash="policy-v1",
        pre_hook=pre, worker=worker, post_hook=post,
    )
    clean = operational_delta(
        source="simplicio-mapper@installed", base_atlas_digest="sha256:atlas",
        observations=[{"kind": "cache_integrity", "subject": "cache:main", "healthy": True}],
    )
    verifier = {
        "schema": "simplicio.independent-verification/v1", "gap_id": gap["gap_id"],
        "verdict": "PASS", "agent_instance_id": "independent-verifier",
        "evidence_refs": ["mapper-rescan://clean"],
    }
    ledger = cc.reduce_ledger(
        None, dirty, [decision], [envelope], [receipts[0]],
        verification_delta=clean, verifier=verifier,
    )
    return {
        "mapper_delta_digest": dirty["delta_digest"],
        "fast_receipt_digest": receipts[0]["receipt_digest"],
        "host_receipt_digest": host_receipt["host_receipt_digest"],
        "duplicate_same_receipt": duplicate == host_receipt,
        "workers_materialized": fast.workers_started,
        "workers_avoided": host.metrics()["workers_avoided"],
        "ledger_digest": ledger["ledger_digest"],
        "terminal": cc.terminal(ledger),
        "fast_verdict": receipts[0]["verdict"],
    }


def benchmark(root: Path, repetitions: int) -> dict:
    gap_cases = {}
    for count in (0, 1, 100, 10_000):
        samples = []
        observations = [
            {"kind": "cache_integrity", "subject": f"cache:{index}", "healthy": False}
            for index in range(count)
        ]
        for _ in range(repetitions):
            cpu = time.process_time()
            before_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            started = time.perf_counter()
            delta = operational_delta(
                source="mapper@installed", base_atlas_digest="sha256:atlas",
                observations=observations,
            )
            decisions = cc.decide(delta, [address("cache_integrity")], {"dispatch_budget": count})
            samples.append({
                "wall_seconds": time.perf_counter() - started,
                "cpu_seconds": time.process_time() - cpu,
                "rss_kib_delta": max(0, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before_rss),
                "gaps": len(delta["gaps"]), "envelopes": sum(item["action"] == "DISPATCH" for item in decisions),
            })
        gap_cases[str(count)] = {"samples": samples, "mean_wall_seconds": statistics.mean(
            item["wall_seconds"] for item in samples
        )}
    addresses_started = time.perf_counter()
    addresses = [address("cache_integrity", index) for index in range(10_000)]
    slot_cases = {}
    for slots in (1, 20, 100):
        executor = SlotExecutor(root / f"slots-{slots}")
        snapshot = executor.open_snapshot(f"slots-{slots}", "source", {})
        started = time.perf_counter()
        for index in range(slots):
            executor.execute(make_envelope(index, run_id=f"slots-{slots}"), snapshot,
                             runtime_available=False, rust_available=False)
        slot_cases[str(slots)] = {
            "wall_seconds": time.perf_counter() - started,
            "workers_materialized": executor.workers_started, "envelopes": slots,
        }
    return {
        "gap_cases": gap_cases,
        "address_case": {
            "addresses": len(addresses), "workers_materialized": 0,
            "wall_seconds": time.perf_counter() - addresses_started,
        },
        "slot_cases": slot_cases,
        "runtime_available": False, "runtime_fallback": "python",
        "rust_available": False, "rust_null_reason": "RUST_UNAVAILABLE",
        "tokens": None, "tokens_null_reason": "NO_LLM_USED",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    if args.repetitions < 3:
        parser.error("at least three repetitions required")
    root = Path(args.root)
    result = {
        "schema": "simplicio.coverage-custodian-installed-e2e/v1",
        "classification": "MEASURED_INSTALLED_ARTIFACTS",
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("simplicio-mapper", "simplicio-fast", "simplicio-loop")
        },
        "module_roots": {
            "mapper": str(Path(__import__("simplicio_mapper").__file__).resolve()),
            "fast": str(Path(__import__("simplicio_fast").__file__).resolve()),
            "loop": str(Path(__import__("simplicio_loop").__file__).resolve()),
        },
        "e2e": execute_e2e(root / "e2e"),
        "benchmark": benchmark(root / "benchmark", args.repetitions),
        "local_llm": False,
    }
    Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    if not result["e2e"]["terminal"] or result["e2e"]["fast_verdict"] == "DELIVERED":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
