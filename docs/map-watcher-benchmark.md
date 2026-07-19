# Map watcher benchmark

Run the centralized watcher receipt with multiple worktrees and clients:

```text
python scripts/benchmark_map_watchers.py --worktrees 8 --clients 4 --events 3 --output bench/map-watcher-baseline.json
```

The benchmark compares the measured one-watcher-per-identity manager with the
naive `worktrees * clients` watcher count. It also verifies that repeated file
events coalesce to one invalidation per worktree and records latency, CPU and
RSS availability. The counts are logical receipts, not a claim of OS-level
filesystem watcher installation.
