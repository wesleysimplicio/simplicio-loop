# Optional uvloop rollout

simplicio_loop.event_loop treats uvloop as an optional Unix-only optimization.

- Windows always uses the standard asyncio policy.
- Unix uses uvloop only when the optional uvloop extra is installed and SIMPLICIO_LOOP_UVLOOP is not disabled.
- SIMPLICIO_LOOP_UVLOOP=0 is the rollback switch; auto is the default.
- Evidence command: python scripts/benchmark_event_loop.py --iterations 1000.
- Compare the JSON receipt selection, throughput and p95 before enabling a canary. Missing uvloop is a safe fallback, never an installation error.

The benchmark is intentionally a small event-loop scheduling baseline; CPU/RSS and workload-specific throughput remain required for a production rollout.
