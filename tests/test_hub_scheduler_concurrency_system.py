import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from simplicio_loop.hub_daemon import (
    HubDaemon,
    HubSocketClient,
    HubSocketServer,
    default_endpoint,
    default_transport,
)


def jains_fairness_index(values):
    total = sum(values)
    total_sq = sum(v * v for v in values)
    if total_sq == 0:
        return 1.0
    return (total * total) / (len(values) * total_sq)


def test_concurrent_clients_submitting_over_real_ipc_socket_are_served_fairly(
    require_default_hub_transport,
) -> None:
    """Multiple simulated clients submit and claim through the real Unix-socket IPC
    transport (HubSocketServer/HubSocketClient), not direct in-memory scheduler calls,
    concurrently from separate threads acting as separate processes would. Proves the
    fairness decision survives the actual daemon dispatch path under concurrency."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        daemon = HubDaemon(str(root / "hub.lock"))
        daemon.start()
        endpoint = default_endpoint(directory)
        transport = default_transport()
        server = HubSocketServer(daemon, endpoint, transport)
        server.start()
        try:
            client_ids = [f"client-{i}" for i in range(6)]
            jobs_per_client = 20

            def submit_all(client_id: str) -> None:
                conn = HubSocketClient(endpoint, transport=transport)
                for index in range(jobs_per_client):
                    response = conn.request(
                        f"{client_id}-submit-{index}",
                        "submit",
                        client_id=client_id,
                        job_id=f"{client_id}-job-{index}",
                    )
                    assert response["ok"], response

            with ThreadPoolExecutor(max_workers=len(client_ids)) as pool:
                list(pool.map(submit_all, client_ids))

            assert daemon.scheduler.status()["global_total"] == len(client_ids) * jobs_per_client

            served = {client_id: 0 for client_id in client_ids}
            claim_conn = HubSocketClient(endpoint, transport=transport)
            total_expected = len(client_ids) * jobs_per_client
            for _ in range(total_expected):
                response = claim_conn.request("claim", "claim_next")
                assert response["ok"], response
                job = response["job"]
                assert job is not None
                served[job["client_id"]] += 1
                complete = claim_conn.request(
                    f"complete-{job['job_id']}", "result", job_id=job["job_id"], result={"ok": True}
                )
                assert complete["ok"]

            assert sum(served.values()) == total_expected
            assert all(count == jobs_per_client for count in served.values())
            fairness = jains_fairness_index(list(served.values()))
            assert fairness > 0.99
        finally:
            server.shutdown()
            daemon.stop()
