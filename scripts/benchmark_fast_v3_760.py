#!/usr/bin/env python3
"""Measure deterministic local lanes; external lanes remain null with reason."""
import argparse, json, platform, resource, statistics, time
from simplicio_loop.fast_v3_delivery import Budget, DeliveryRun, FastV3Runner

def one(full=False):
    run = DeliveryRun("verify fixture", ["focused", "full"], "fixture", "abc", "g1",
                      Budget(3, 100, 4096))
    runner = FastV3Runner(
        orient=lambda task, budget: {"status": "READY", "provider": "fixture-fast",
                                     "handles": [{"handle": "fixture:a", "content": "a"}]},
        verify=lambda scope: {"ok": True, "scope": scope},
        authorize=(lambda request: {"ok": True, "provider": "fixture-runtime"}) if full else None)
    wall, cpu, before = time.perf_counter_ns(), time.process_time_ns(), resource.getrusage(resource.RUSAGE_SELF)
    result = runner.execute(run, verify_only=True, full=full)
    after = resource.getrusage(resource.RUSAGE_SELF)
    return {"success": result["sealed"], "acceptance_coverage": 1.0,
            "wall_ns": time.perf_counter_ns()-wall, "cpu_ns": time.process_time_ns()-cpu,
            "rss_kib": after.ru_maxrss,
            "page_faults": (after.ru_minflt+after.ru_majflt)-(before.ru_minflt+before.ru_majflt),
            "context_bytes": result["context_bytes"], "llm_calls": 0,
            "input_tokens": None, "output_tokens": None, "reasoning_tokens": None, "cache_tokens": None}

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("--repetitions",type=int,default=10); p.add_argument("--output",default="")
    a=p.parse_args(argv)
    if a.repetitions < 10: p.error("at least 10 repetitions are required")
    lanes={}
    for scenario,full in (("S0",False),("S1",False),("S3",True)):
        raw=[one(full) for _ in range(a.repetitions)]
        lanes[scenario]={"status":"measured","raw":raw,
                         "median_wall_ns":statistics.median(x["wall_ns"] for x in raw),
                         "median_cpu_ns":statistics.median(x["cpu_ns"] for x in raw)}
    lanes["S2"]={"status":"unavailable","measurements":None,
                 "reason":"Rust Fast binary/provider unavailable in hermetic fixture"}
    lanes["S4"]={"status":"unavailable","measurements":None,
                 "reason":"Rust Fast plus real Runtime provider unavailable in hermetic fixture"}
    payload={"schema":"simplicio.loop.fast-v3-benchmark/v1",
             "environment":{"python":platform.python_version(),"platform":platform.platform()},
             "repetitions":a.repetitions,"lanes":lanes,"claim":None,
             "claim_reason":"fixture timings do not establish product speedup"}
    text=json.dumps(payload,indent=2)+"\n"
    if a.output: open(a.output,"w",encoding="utf-8").write(text)
    print(text,end=""); return 0
if __name__=="__main__": raise SystemExit(main())
