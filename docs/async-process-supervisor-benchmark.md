# Async process supervisor benchmark

Issue #509 is measured with real `python -c` subprocesses, recreating the
supervisor between rounds. The benchmark records throughput, batch p95,
process CPU time, RSS when `psutil` is available, and duplicate outcomes.

```powershell
python scripts/benchmark_async_process_supervisor.py --rounds 5 --concurrency 4 --output bench/async-process-supervisor-baseline.json
```

`rss_bytes` is `null` with `rss_source=unavailable` when the host has no
portable RSS provider. That is an explicit environment limitation, not a
synthetic measurement. Persisted idempotency outcomes prevent replaying a
completed key after supervisor recreation; abandoned leases are surfaced in
`status()["recovered_leases"]` for an explicit retry policy.
