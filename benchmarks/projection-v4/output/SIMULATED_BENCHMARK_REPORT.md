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

## Projected quantization outcomes at 1m vectors

> Q0/Q1/Q2 results are also **SIMULATED**. Q2a isolates 4-bit retrieval; Q2b adds integral re-ranking.

| Lane | Query p50 | Index | Index reduction | RSS reduction | Recall@10 | nDCG@10 |
|---|---:|---:|---:|---:|---:|---:|
| Q0 full precision | 35.02 ms | 3050 MB | 0.0% | 0.0% | 0.990 | 0.990 |
| Q1 int8 | 25.83 ms | 1067 MB | 65.0% | 45.1% | 0.975 | 0.980 |
| Q2a TurboQuant 4-bit | 16.81 ms | 549 MB | 82.0% | 66.0% | 0.930 | 0.940 |
| Q2b 4-bit + integral rerank | 21.77 ms | 549 MB | 82.0% | 66.1% | 0.985 | 0.988 |

## Method

A deterministic Monte Carlo model samples phase duration, token volume, failures and retries.
Parallel capacity is bounded by scenario capacity and reduced by workload conflict ratio.
The quant matrix uses identical corpus inputs across Q0 full precision, Q1 int8, Q2a TurboQuant 4-bit and Q2b 4-bit with integral re-ranking.
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
- Quant quality values are assumptions until measured on identical corpus, queries, embeddings and hardware.
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
- [Quant benchmark issue simplicio-fast #198](https://github.com/wesleysimplicio/simplicio-fast/issues/198)
