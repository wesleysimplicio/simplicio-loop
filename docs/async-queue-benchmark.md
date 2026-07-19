# Bounded queue benchmark

Run the event-driven queue benchmark and retain its JSON receipt:

    python scripts/benchmark_async_queue.py --items 100 --capacity 4 --repeats 5 --output bench/async-queue-baseline.json

The receipt records throughput, p95, CPU, peak RSS, queue capacity, accepted items, waits and an idle CPU sample. A bounded queue must keep `queue.max_items` fixed and `queue.items` at zero after the drain. The idle sample exercises an empty consumer waiting on an event/condition; it does not use a polling loop.
