"""Epic #496 end-to-end system test: Hub daemon + real socket transport +
fair scheduler dispatch order + resource governor admission, driven together
as one pipeline instead of each module's own isolated unit tests.

The daemon does not (yet) wire ``FairScheduler``/``ResourceGovernor``
internally, so this test plays the role production wiring will eventually
play: it is the client-and-orchestrator side of the pipeline, talking to a
real ``HubSocketServer`` over a real Unix socket, while a shared
``FairScheduler`` decides dispatch order and a shared ``ResourceGovernor``
admits or denies each dispatch before the corresponding daemon IPC calls run.
"""

import os
import tempfile
import threading
from pathlib import Path
from typing import Dict, List

import pytest

from simplicio_loop.hub_daemon import HubDaemon, HubSocketClient, HubSocketServer
from simplicio_loop.hub_governor import (
    ResourceGovernor,
    ResourceLimits,
    ResourceRequest,
    ResourceThrottled,
)
from simplicio_loop.hub_scheduler import FairScheduler, ScheduledJob

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Unix socket pipeline runs on POSIX")


GOOD_CLIENTS = ["client-0", "client-1", "client-2"]
OVER_BUDGET_CLIENT = "client-3"
JOBS_PER_CLIENT = 5


def _submit_all(socket_path: str, client_id: str, job_ids: List[str], errors: List[str]) -> None:
    client = HubSocketClient(socket_path, timeout=10.0)
    try:
        reg = client.request("reg-%s" % client_id, "register", client_id=client_id)
        if not reg.get("ok"):
            errors.append("register failed for %s: %r" % (client_id, reg))
            return
        for job_id in job_ids:
            sub = client.request("sub-%s" % job_id, "submit", client_id=client_id, job_id=job_id)
            if not sub.get("ok") or sub["job"]["state"] != "queued":
                errors.append("submit failed for %s: %r" % (job_id, sub))
    except OSError as exc:
        errors.append("transport error for %s: %s" % (client_id, exc))


def test_hub_pipeline_multi_client_fair_dispatch_and_governor_denial(require_af_unix) -> None:
    with tempfile.TemporaryDirectory() as directory:
        lock_path = str(Path(directory) / "hub.lock")
        socket_path = str(Path(directory) / "hub.sock")

        daemon = HubDaemon(lock_path)
        daemon.start()
        server = HubSocketServer(daemon, socket_path)
        server.start()

        try:
            all_clients = GOOD_CLIENTS + [OVER_BUDGET_CLIENT]
            job_ids: Dict[str, List[str]] = {
                client_id: ["%s-job-%d" % (client_id, i) for i in range(JOBS_PER_CLIENT)]
                for client_id in all_clients
            }

            submit_errors: List[str] = []
            submit_threads = [
                threading.Thread(target=_submit_all, args=(socket_path, client_id, job_ids[client_id], submit_errors))
                for client_id in all_clients
            ]
            for thread in submit_threads:
                thread.start()
            for thread in submit_threads:
                thread.join(timeout=15)
            assert submit_errors == []
            assert daemon.queue.count() == len(all_clients) * JOBS_PER_CLIENT

            scheduler = FairScheduler(max_inflight_per_client=2, quantum=1)
            for client_id in all_clients:
                for job_id in job_ids[client_id]:
                    scheduler.enqueue(ScheduledJob(task_id=job_id, client_id=client_id, weight=1, cost=1))

            governor = ResourceGovernor(
                ResourceLimits(cpu=1000),
                client_limits={
                    OVER_BUDGET_CLIENT: ResourceLimits(cpu=1),
                    **{client_id: ResourceLimits(cpu=1000) for client_id in GOOD_CLIENTS},
                },
            )

            dispatch_order: List[str] = []
            completed: Dict[str, List[str]] = {client_id: [] for client_id in all_clients}
            denied: Dict[str, List[str]] = {client_id: [] for client_id in all_clients}
            pipeline_client = HubSocketClient(socket_path, timeout=10.0)

            remaining = len(all_clients) * JOBS_PER_CLIENT
            stalls = 0
            while remaining > 0:
                job = scheduler.next()
                if job is None:
                    stalls += 1
                    assert stalls < 10_000, "scheduler stalled without making progress"
                    continue
                dispatch_order.append(job.client_id)
                request = ResourceRequest(cpu=2 if job.client_id == OVER_BUDGET_CLIENT else 1)
                try:
                    lease = governor.admit(job.client_id, job.task_id, request)
                except ResourceThrottled:
                    denied[job.client_id].append(job.task_id)
                    cancel = pipeline_client.request(
                        "cancel-%s" % job.task_id, "cancel", client_id=job.client_id, job_id=job.task_id
                    )
                    assert cancel["ok"] is True
                    scheduler.complete(job.task_id)
                    remaining -= 1
                    continue
                try:
                    claim = pipeline_client.request(
                        "claim-%s" % job.task_id, "claim", client_id=job.client_id, job_id=job.task_id
                    )
                    assert claim["ok"] is True and claim["job"]["state"] == "claimed"
                    progress = pipeline_client.request(
                        "prog-%s" % job.task_id, "progress", client_id=job.client_id, job_id=job.task_id, progress=50
                    )
                    assert progress["ok"] is True
                    result = pipeline_client.request(
                        "res-%s" % job.task_id, "result", client_id=job.client_id, job_id=job.task_id,
                        result={"ok": True},
                    )
                    assert result["ok"] is True and result["job"]["state"] == "completed"
                    completed[job.client_id].append(job.task_id)
                finally:
                    governor.release(lease)
                    scheduler.complete(job.task_id)
                    remaining -= 1

            for client_id in GOOD_CLIENTS:
                assert sorted(completed[client_id]) == sorted(job_ids[client_id])
                assert denied[client_id] == []

            assert completed[OVER_BUDGET_CLIENT] == []
            assert sorted(denied[OVER_BUDGET_CLIENT]) == sorted(job_ids[OVER_BUDGET_CLIENT])

            for job_id in job_ids[OVER_BUDGET_CLIENT]:
                row = daemon.queue.get_row(daemon.queue.find_task_id(job_id))
                assert row["payload"]["state"] == "cancelled"
            for client_id in GOOD_CLIENTS:
                for job_id in job_ids[client_id]:
                    row = daemon.queue.get_row(daemon.queue.find_task_id(job_id))
                    assert row["payload"]["state"] == "completed"

            first_round = dispatch_order[: len(GOOD_CLIENTS)]
            assert set(first_round) == set(GOOD_CLIENTS), (
                "fairness violated: good clients should each get an early dispatch slot, got %r" % dispatch_order
            )

            scheduler_status = scheduler.status()
            assert scheduler_status["queued"] == 0
            assert scheduler_status["starvation_preventions"] == 0

            governor_status = governor.status()
            assert governor_status["active_leases"] == 0
            assert governor_status["throttle_receipts"] == JOBS_PER_CLIENT
            receipts = governor.receipts()
            assert all(r["reason"] == "client_budget" for r in receipts)
            assert all(r["client_id"] == OVER_BUDGET_CLIENT for r in receipts)
        finally:
            server.shutdown()
            daemon.stop()
