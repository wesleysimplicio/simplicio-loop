#!/usr/bin/env python3
"""Reproducible hot-path benchmark for issue #617 binding validation."""
import argparse, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from simplicio_loop.evidence_binding import bind_receipt, validate_receipt_binding

def main():
    p=argparse.ArgumentParser(); p.add_argument("--iterations", type=int, default=100000); a=p.parse_args()
    binding={"schema":"simplicio.evidence-binding/v1","run_id":"r","task_id":"t","attempt_id":"a",
      "head_hash":"h","tree_hash":"t","diff_hash":"d","policy_hash":"p","config_hash":"c",
      "toolchain_hash":"x","task_contract_hash":"q"}
    from simplicio_loop.evidence_binding import content_hash
    binding["binding_hash"]=content_hash(binding); receipt=bind_receipt({},binding)
    start=time.perf_counter_ns()
    for _ in range(a.iterations): validate_receipt_binding(receipt,binding)
    elapsed=time.perf_counter_ns()-start
    print(json.dumps({"schema":"simplicio.evidence-binding-bench/v1","iterations":a.iterations,
      "elapsed_ms":elapsed/1e6,"ns_per_validation":elapsed/a.iterations,"throughput_per_second":a.iterations/(elapsed/1e9)}))
if __name__ == "__main__": main()
