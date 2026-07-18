# Optional uvloop rollout

simplicio_loop.event_loop treats uvloop as an optional Unix-only optimization.

- Windows always uses the standard asyncio policy.
- Unix uses uvloop only when the optional uvloop extra is installed and SIMPLICIO_LOOP_UVLOOP is not disabled.
- SIMPLICIO_LOOP_UVLOOP=0 is the rollback switch; auto is the default.
- Benchmark receipts include workload, throughput, p95, process CPU time/percent and peak RSS (resource or tracemalloc fallback).
- Reproduce a versioned baseline with:
  python scripts/benchmark_event_loop.py --iterations 1000 --workload gather --output bench/event-loop-baseline.json
- A canary is explicit: run the same command with SIMPLICIO_LOOP_UVLOOP=1 on Unix, compare the JSON fields against the baseline, and enable only after review.
- Roll back immediately with SIMPLICIO_LOOP_UVLOOP=0; missing uvloop is always a safe fallback and never an installation error.

The committed benchmark artifact records the Windows/asyncio baseline. It is not a claim of Unix uvloop performance; Unix canary evidence must be produced on Unix with the optional extra installed.
