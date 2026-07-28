"""Measured offline HBP verification benchmark; no provider/LLM is invoked."""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simplicio_loop.hbp_ledger import (
    AcceptanceEvidence, GENESIS_HASH, HbpBinding, build_receipt,
    canonical_sha256, completion_oracle,
)


def execute(runs: int, chain_length: int) -> dict:
    binding = HbpBinding("bench-813", "plan", "g1", 1, 3, "verify")
    receipts = []
    previous = GENESIS_HASH
    for sequence in range(1, chain_length + 1):
        ac_id = f"AC{sequence}"
        receipt = build_receipt(
            sequence=sequence, binding=binding,
            previous_receipt_hash=previous,
            evidence=[AcceptanceEvidence(
                ac_id, f"artifact://{sequence}", canonical_sha256({"ac": ac_id})
            )],
            payload={"sequence": sequence}, observed_at_ns=sequence,
        )
        receipts.append(receipt)
        previous = receipt["receipt_hash"]
    samples = []
    verdict = None
    criteria = [f"AC{index}" for index in range(1, chain_length + 1)]
    for _ in range(runs):
        started = time.perf_counter_ns()
        verdict = completion_oracle(
            receipts, expected=binding, acceptance_criteria=criteria
        )
        samples.append(time.perf_counter_ns() - started)
    root = Path(__file__).resolve().parents[1]
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                         capture_output=True, text=True, check=False)
    payload = {
        "schema": "simplicio.hbp-benchmark/v1",
        "classification": "MEASURED_LOCAL",
        "runs": runs, "chain_length": chain_length,
        "verdict": verdict["verdict"],
        "latency_ns": {
            "p50": statistics.median(samples),
            "p95": sorted(samples)[max(0, int(runs * .95) - 1)],
            "min": min(samples), "max": max(samples),
        },
        "environment": {
            "python": sys.version.split()[0], "platform": sys.platform,
            "source_sha": git.stdout.strip() if git.returncode == 0 else None,
            "source_sha_reason": None if git.returncode == 0 else "git SHA unavailable",
        },
        "provider_metrics": None,
        "provider_metrics_reason": "offline verifier does not invoke a provider",
        "local_llm": False,
    }
    payload["receipt_hash"] = canonical_sha256(payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1000)
    parser.add_argument("--chain-length", type=int, default=20)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.runs < 100 or args.chain_length < 1:
        parser.error("runs >= 100 and chain-length >= 1 are required")
    payload = execute(args.runs, args.chain_length)
    Path(args.output).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
