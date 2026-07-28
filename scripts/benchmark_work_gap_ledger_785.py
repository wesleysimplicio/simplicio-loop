#!/usr/bin/env python3
"""Measured 1/20/100/600-item Work Gap Ledger stress report."""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simplicio_loop.work_gap_ledger import (  # noqa: E402
    WorkGap, WorkGapLedger, sha256_evidence, validate_work_gap_snapshot,
)


def evidence(kind, actor):
    return sha256_evidence(kind, f"stress:{kind}", kind.encode(), actor)


def run(count, *, include_snapshot=False):
    started = time.perf_counter_ns()
    ledger = WorkGapLedger()
    for number in range(count):
        gap = WorkGap(
            "REQ-STRESS", f"AC-{number:04d}",
            expected_evidence=("implementation", "verification", "integration", "delivery"),
            delivery_target="package:simplicio-loop",
            expected_revision="stress-commit",
        )
        ledger.register(gap)
        ledger.assign_owner(
            gap.key, owner_project="simplicio-loop", owner_agent="owner",
            actor_id="coverage",
        )
        ledger.transition(gap.key, "PLANNED", actor_id="planner", seat="planner")
        ledger.transition(
            gap.key, "IMPLEMENTED", actor_id="executor", seat="executor",
            executor_id="executor", evidence=(evidence("implementation", "executor"),),
        )
        ledger.transition(
            gap.key, "VERIFIED", actor_id="verifier", seat="verifier",
            verifier_id="verifier", evidence=(evidence("verification", "verifier"),),
        )
        ledger.transition(
            gap.key, "INTEGRATED", actor_id="integrator", seat="integration",
            evidence=(evidence("integration", "integrator"),),
        )
        ledger.transition(
            gap.key, "DELIVERED", actor_id="auditor", seat="completion",
            completion_auditor_id="auditor",
            evidence=(evidence("delivery", "auditor"),),
            installed_artifact={
                "expected_commit": "stress-commit",
                "installed_commit": "stress-commit",
                "sha256": "a" * 64, "match": True,
            },
            source_requery={"commit": "stress-commit", "state": "merged"},
        )
    snapshot = ledger.snapshot()
    validation = validate_work_gap_snapshot(snapshot)
    elapsed = time.perf_counter_ns() - started
    if not validation["ok"] or ledger.unresolved():
        raise RuntimeError(validation["detail"]["errors"])
    result = {
        "items": count, "events": len(ledger.events), "lost_items": 0,
        "duration_ns": elapsed, "ledger_digest": ledger.digest(),
        "replay_digest": validation["detail"]["digest"],
    }
    if include_snapshot:
        result["snapshot"] = snapshot
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rows = []
    for count in (1, 20, 100, 600):
        samples = [run(count) for _ in range(args.repeats)]
        rows.append({
            **samples[-1],
            "repeats": args.repeats,
            "duration_median_ns": int(statistics.median(
                sample["duration_ns"] for sample in samples
            )),
            "duration_min_ns": min(sample["duration_ns"] for sample in samples),
            "duration_max_ns": max(sample["duration_ns"] for sample in samples),
        })
    payload = {
        "schema": "simplicio.work-gap-ledger-stress/v1",
        "classification": "MEASURED_LOCAL",
        "local_llm": False,
        "rows": rows,
        "cpu_seconds": None,
        "rss_peak_bytes": None,
        "null_reason": "portable per-operation CPU and RSS attribution unavailable",
        "clean_control": run(1, include_snapshot=True)["snapshot"],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["receipt_hash"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
    encoded = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
