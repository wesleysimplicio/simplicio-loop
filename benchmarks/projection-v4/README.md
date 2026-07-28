# Simplicio Loop v4 projection benchmark

This package is a reusable Monte Carlo projection for the architecture tracked
by `simplicio-loop#801`, including the explicit Q0/Q1/Q2 quantization matrix
tracked by `simplicio-fast#198`.

## Classification

All performance, cost, token, throughput and quality outputs are `SIMULATED`.
Release metadata and repository links are observed inputs. The benchmark must
never be quoted as measured production evidence.

## Run

```bash
python3 run_projection.py --assumptions assumptions.json --output output
```

The command generates:

- compressed raw samples;
- workflow and quantization summary CSVs;
- a simulation manifest;
- nine chart images, including latency, footprint and quality quant views;
- a Markdown report;
- a rendered PDF report.

Change `assumptions.json` to calibrate the model with real receipts. Keep the
seed fixed when comparing model changes, and change it only for sensitivity
analysis.

## Promotion rule

Replace assumed phase distributions with measured data from issue #816. A
simulated value cannot become a README or release claim without a reproducible
measured dataset, identical workload inputs and quality parity.

For quantization, promotion additionally requires the same corpus, queries,
embeddings and hardware across Q0 full precision, Q1 int8, Q2a TurboQuant
4-bit and Q2b 4-bit with integral re-ranking.
