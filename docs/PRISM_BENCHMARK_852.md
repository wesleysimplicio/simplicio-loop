# Prism benchmark #852

This benchmark is a measured, local, model-free comparison of serial,
legacy-bounded fan-out, and the Python Prism scheduler. It freezes three loads:
1×10, 4×10, and 20×10 logical tasks. Each applicable scenario has one excluded
warmup and at least ten measured repetitions.

Run it without paid GitHub Actions:

```bash
python3 bench/prism_benchmark_852.py \
  --repetitions 10 \
  --physical-cap 20 \
  --output bench/results/prism-benchmark-852.json
```

Every raw repetition runs the correctness oracle before its timing contributes
to the summary. The oracle rejects lost tasks, unexpected tasks, non-accepted
states, duplicate invocation, and physical overlap above the configured cap.
Two synthetic conflicting tasks per slot exercise selective serialization.

S0 is serial. S1 is the bounded legacy fan-out control arm. S2 is the Python
Prism path. S3 is reserved for a Runtime binary implementing the frozen Prism
benchmark protocol. Merely finding a Rust executable is not treated as parity:
until that protocol exists, S3 remains `measured=false` with a reason. S4 is
the explicitly reasoned Python fallback and binds to the same verified S2
result.

The checked-in JSON is raw measured evidence, not a projection. Unavailable CPU
per-task, portable I/O/network, provider/model, Mapper precision/recall, and
Rust module-load metrics remain `null` or carry explicit reasons. Fault
evidence is linked to the recovery, budgets, reducer, and fencing tests instead
of being invented by the latency harness.
