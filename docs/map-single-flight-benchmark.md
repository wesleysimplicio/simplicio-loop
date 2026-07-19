# Map single-flight benchmark

Run the reproducible shared-build benchmark and retain its JSON receipt:

```text
python scripts/benchmark_map_single_flight.py --clients 24 --repeats 5 --output bench/map-single-flight-baseline.json
```

The receipt measures client fan-in, one builder call per invalidated round,
logical snapshot I/O, mean/p95 latency, CPU time, and peak RSS. `builder_calls`
is the directly measured proxy for duplicate mapper work; it must equal
`repeats`, while every client in a round must receive the same cache key.
