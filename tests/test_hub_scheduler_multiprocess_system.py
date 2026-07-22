"""Hermetic real-process/real-AF_UNIX fairness proof for issue #635."""
import multiprocessing as mp
import time
from pathlib import Path

import pytest

from simplicio_loop.hub_daemon import HubDaemon, HubSocketClient, HubSocketServer, default_endpoint, default_transport


def _producer(endpoint, transport, barrier, done, remaining, client_id, count):
    client = HubSocketClient(endpoint, transport=transport)
    barrier.wait()
    for index in range(count):
        result = client.request(
            f"submit-{client_id}-{index}", "hub_submit", payload={"producer": client_id, "index": index},
            idempotency_key=f"{client_id}-{index}", client_id=client_id,
        )
        assert result["ok"]
    with remaining.get_lock():
        remaining.value -= 1
        if remaining.value == 0:
            done.set()


def _consumer(endpoint, transport, barrier, done, count, output, worker_id):
    client = HubSocketClient(endpoint, transport=transport)
    barrier.wait()
    assert done.wait(20)
    waits = []
    served = {}
    for index in range(count):
        started = time.perf_counter()
        response = client.request(
            f"claim-{worker_id}-{index}", "hub_claim", worker_id=worker_id, request={}
        )
        waits.append((time.perf_counter() - started) * 1000)
        claimed = response["claimed"]
        assert claimed is not None
        served[claimed["client_id"]] = served.get(claimed["client_id"], 0) + 1
        assert client.request(
            f"complete-{worker_id}-{index}", "hub_complete", task_id=claimed["task_id"]
        )["ok"]
    output.put((served, waits))


def test_real_process_producers_consumers_no_starvation_and_bounded_burst(
    tmp_path: Path, require_default_hub_transport
) -> None:
    transport = default_transport()
    if transport != "unix":
        pytest.skip("reason_code=af_unix_unavailable")
    daemon = HubDaemon(str(tmp_path / "hub.lock"))
    daemon.start()
    endpoint = default_endpoint(str(tmp_path))
    server = HubSocketServer(daemon, endpoint, transport)
    server.start()
    context = mp.get_context("spawn")
    producer_count, consumer_count = 4, 2
    jobs = {"heavy-a": 80, "heavy-b": 80, "light-a": 20, "light-b": 20}
    barrier = context.Barrier(producer_count + consumer_count)
    done = context.Event()
    remaining = context.Value("i", producer_count)
    output = context.Queue()
    processes = [
        context.Process(target=_producer, args=(endpoint, transport, barrier, done, remaining, client, count))
        for client, count in jobs.items()
    ] + [
        context.Process(target=_consumer, args=(endpoint, transport, barrier, done, sum(jobs.values()) // consumer_count, output, f"worker-{i}"))
        for i in range(consumer_count)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(30)
            assert process.exitcode == 0
        results = [output.get(timeout=2) for _ in range(consumer_count)]
        served = {client: 0 for client in jobs}
        waits = []
        for counts, samples in results:
            waits.extend(samples)
            for client, count in counts.items():
                served[client] += count
        assert served == jobs
        assert max(waits) < 2000
        status = daemon.scheduler.status()
        assert status["global_total"] == 0
        assert status["jains_fairness_index"] > 0.73
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
        server.shutdown()
        daemon.stop()
