"""Measured queue/fairness benchmark for issue #676."""
from __future__ import annotations

import argparse
import json
import time

from simplicio_loop.inference_admission import AdmissionJob, CapacityLimits, FairAdmissionController


def jain(values):
    total = sum(values)
    squared = sum(value * value for value in values)
    return 1.0 if not squared else (total * total) / (len(values) * squared)


def benchmark(clients=4, jobs_per_client=100, queue_limit=64):
    controller = FairAdmissionController(
        CapacityLimits(
            max_runnable=1,
            max_active_workers=1,
            max_inference_requests=1,
            max_backend_slots=1,
            max_queue=queue_limit,
        ),
        aging_ticks=8,
    )
    served = {str(client): 0 for client in range(clients)}
    rejected = deferred = 0
    active_max = 0
    started = time.perf_counter()
    for index in range(clients * jobs_per_client):
        client = str(index % clients)
        decision = controller.submit(
            AdmissionJob("job-%d" % index, client, "session-%s" % client, priority="background")
        )
        if decision.state == "rejected":
            rejected += 1
        elif decision.state == "deferred":
            deferred += 1
        active_max = max(active_max, controller.status()["usage"]["active_workers"])
    controller.release("job-0")
    dispatch_started = time.perf_counter()
    while controller.queued:
        current = controller.next()
        if current is None:
            break
        served[current.client_id] += 1
        active_max = max(active_max, controller.status()["usage"]["active_workers"])
        controller.release(current.job_id)
    elapsed_ms = (time.perf_counter() - started) * 1000
    dispatch_ms = (time.perf_counter() - dispatch_started) * 1000
    status = controller.status()
    return {
        "schema": "simplicio.inference-admission-benchmark/v1",
        "clients": clients,
        "jobs_per_client": jobs_per_client,
        "submitted": clients * jobs_per_client,
        "deferred": deferred,
        "rejected": rejected,
        "served": served,
        "queue_empty": status["queued"] == 0,
        "max_active_workers": active_max,
        "jains_fairness_index": jain(list(served.values())),
        "starvation_preventions": status["starvation_preventions"],
        "elapsed_ms": round(elapsed_ms, 3),
        "dispatch_ms": round(dispatch_ms, 3),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument("--jobs-per-client", type=int, default=100)
    parser.add_argument("--queue-limit", type=int, default=64)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.clients, args.jobs_per_client, args.queue_limit), sort_keys=True))


if __name__ == "__main__":
    main()