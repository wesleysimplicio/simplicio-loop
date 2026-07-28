# Simplicio Loop 4.0 - Simulated Benchmark Projection

> Classification: **SIMULATED**. This document is not evidence of measured production gains.

- Baseline release metadata: `3.38.5`
- Baseline main SHA observed during design: `16b8f94cf31a719cb7c48fe320d87ec88e8b663a`
- Projection: `4.0.0 - all issues in epic #801 completed`
- Seed: `20260728`
- Repetitions per scenario/workload: `2500`

## Projected S4 outcomes

| Workload | p50 duration | p95 duration | Token reduction | Completion rate | Throughput |
|---|---:|---:|---:|---:|---:|
| Mechanical change | 69 s | 81 s | 72.5% | 99.5% | 52.45 tasks/h |
| Cross-module change | 3.9 min | 4.8 min | 72.3% | 96.3% | 15.39 tasks/h |
| 20 independent issues | 3.2 min | 4.2 min | 72.5% | 95.4% | 369.46 tasks/h |
| 100 issues with conflicts | 9.3 min | 12.9 min | 72.5% | 90.4% | 641.78 tasks/h |
| Crash and recovery | 3.7 min | 5.4 min | 72.3% | 84.5% | 80.73 tasks/h |

## Method

A deterministic Monte Carlo model samples phase duration, token volume, failures and retries.
Parallel capacity is bounded by scenario capacity and reduced by workload conflict ratio.
Every coefficient is editable in `assumptions.json`; rerun the script to regenerate all outputs.

## Interpretation rules

- `MEASURED`: repository release metadata only.
- `OBSERVED`: architecture and issue scope found in the repository.
- `SIMULATED`: every duration, token, cost, throughput and completion result.
- `TARGET`: desired release behavior, not proof.

## Important limitations

- Phase baselines are calibration assumptions, not timings from production receipts.
- The blended USD token rate is a normalization input, not a provider price claim.
- Network, repository shape, model behavior and test suites can dominate real results.
- The simulation must be replaced progressively with measured distributions from issue #816.

## Reproduce

```bash
python3 run_projection.py --assumptions assumptions.json --output output
```

## Sources

- [simplicio-loop pyproject.toml](https://github.com/wesleysimplicio/simplicio-loop/blob/main/pyproject.toml)
- [simplicio-loop CHANGELOG.md](https://github.com/wesleysimplicio/simplicio-loop/blob/main/CHANGELOG.md)
- [Projection epic #801](https://github.com/wesleysimplicio/simplicio-loop/issues/801)
- [Submodule PR #817](https://github.com/wesleysimplicio/simplicio-loop/pull/817)
