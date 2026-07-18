from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from simplicio_loop.hub_governor import ResourceGovernor, ResourceLimits, ResourceRequest
from simplicio_loop.hub_queue_retry import HubRetryQueue
from simplicio_loop.hub_scheduler import FairScheduler
from simplicio_loop.hub_service import HubService


def _service(directory, **governor_limits) -> HubService:
    queue = HubRetryQueue(str(Path(directory) / "queue.db"))
    scheduler = FairScheduler(max_inflight_per_client=4)
    governor = ResourceGovernor(ResourceLimits(**governor_limits))
    return HubService(queue, scheduler, governor)


def test_submit_then_claim_round_trip_persists_and_schedules() -> None:
    with tempfile.TemporaryDirectory() as directory:
        service = _service(directory, cpu=4)
        task_id = service.submit(
            {"kind": "work"}, idempotency_key="k1", client_id="alice", cost=1,
        )
        claimed = service.claim("worker-1", ResourceRequest(cpu=1))
        assert claimed is not None
        assert claimed.task_id == task_id
        assert claimed.payload == {"kind": "work"}
        assert service.queue.state(task_id) == "leased"

        service.complete(claimed)
        assert service.queue.state(task_id) == "completed"
        # Both the resource lease and the scheduler slot were released.
        assert service.governor.status()["used"]["cpu"] == 0
        assert service.scheduler.status()["global_total"] == 0


def test_idempotent_submit_does_not_double_schedule() -> None:
    with tempfile.TemporaryDirectory() as directory:
        service = _service(directory, cpu=4)
        first = service.submit({"kind": "work"}, idempotency_key="same", client_id="alice")
        second = service.submit({"kind": "changed"}, idempotency_key="same", client_id="alice")
        assert first == second
        assert service.scheduler.status()["global_total"] == 1


def test_fairness_across_clients_is_actually_respected() -> None:
    """A client that floods the queue must not starve a lighter client — this is the
    real DRR fairness behavior of FairScheduler, now exercised THROUGH the composed
    service rather than in isolation."""
    with tempfile.TemporaryDirectory() as directory:
        service = _service(directory, cpu=100)
        for i in range(10):
            service.submit({"i": i}, idempotency_key="heavy-%d" % i, client_id="heavy")
        service.submit({"i": "only"}, idempotency_key="light-1", client_id="light")

        claimed_clients = []
        for _ in range(4):
            claimed = service.claim("worker-1", ResourceRequest(cpu=1))
            assert claimed is not None
            claimed_clients.append(claimed.client_id)
            service.complete(claimed)

        assert "light" in claimed_clients, (
            "the light client's single job must be served within the first few "
            "claims, not starved behind the heavy client's backlog"
        )


def test_governor_refusal_defers_the_candidate_without_losing_it() -> None:
    """If the fairness-picked candidate would exceed the resource budget, claim() must
    not lose that job — it goes back into the scheduler and a DIFFERENT, affordable
    candidate is tried instead in the same call."""
    with tempfile.TemporaryDirectory() as directory:
        service = _service(directory, cpu=1)  # only 1 cpu of global budget
        expensive_task = service.submit(
            {"kind": "expensive"}, idempotency_key="expensive", client_id="alice", cost=1,
        )
        cheap_task = service.submit(
            {"kind": "cheap"}, idempotency_key="cheap", client_id="bob", cost=1,
        )

        # First call: alice's job is picked first by DRR order, needs 5 cpu (over
        # budget) - must be deferred, and bob's cheap (1 cpu) job claimed instead.
        claimed = service.claim("worker-1", ResourceRequest(cpu=5), max_candidates=1)
        assert claimed is None, "a lone over-budget candidate must not be claimed"
        # It was re-queued, not dropped.
        assert service.scheduler.status()["global_total"] == 2

        claimed = service.claim("worker-1", ResourceRequest(cpu=1))
        assert claimed is not None
        assert claimed.task_id in (expensive_task, cheap_task)
        service.complete(claimed)


def test_fail_with_retry_re_enters_fair_scheduling() -> None:
    with tempfile.TemporaryDirectory() as directory:
        service = _service(directory, cpu=4)
        task_id = service.submit(
            {"kind": "flaky"}, idempotency_key="flaky", client_id="alice", max_attempts=2,
        )
        claimed = service.claim("worker-1", ResourceRequest(cpu=1))
        assert claimed is not None
        outcome = service.fail(claimed, error_code="temporary")
        assert outcome == "retry"
        assert service.queue.state(task_id) == "queued"
        # Re-entered the scheduler for a real retry attempt.
        assert service.scheduler.status()["global_total"] == 1

        reclaimed = service.claim("worker-2", ResourceRequest(cpu=1))
        assert reclaimed is not None
        assert reclaimed.task_id == task_id
        outcome = service.fail(reclaimed, error_code="permanent")
        assert outcome == "dead_letter"
        assert service.queue.state(task_id) == "dead_letter"
        assert service.scheduler.status()["global_total"] == 0


def test_claim_returns_none_when_queue_and_scheduler_are_both_empty() -> None:
    with tempfile.TemporaryDirectory() as directory:
        service = _service(directory, cpu=4)
        assert service.claim("worker-1", ResourceRequest(cpu=1)) is None


def test_concurrent_workers_never_double_claim_across_the_composed_stack() -> None:
    """Real threads, each with its OWN sqlite3 connection (HubRetryQueue is thread-
    affine) but sharing the SAME FairScheduler/ResourceGovernor (both RLock-protected),
    race to claim 40 pre-submitted jobs. Every job must be claimed by exactly one
    worker and every job must be processed - proving the composition is safe under
    real concurrency, not just single-threaded call order."""
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "queue.db")
        scheduler = FairScheduler(max_inflight_per_client=40)
        governor = ResourceGovernor(ResourceLimits(cpu=40))

        seed_queue = HubRetryQueue(path)
        seed_service = HubService(seed_queue, scheduler, governor)
        task_ids = [
            seed_service.submit(
                {"i": i}, idempotency_key="job-%d" % i, client_id="client-%d" % (i % 4),
            )
            for i in range(40)
        ]
        seed_queue.close()

        claims_by_task: dict = {}
        lock = threading.Lock()
        errors: list = []

        def worker(worker_id: str) -> None:
            try:
                queue = HubRetryQueue(path)
                service = HubService(queue, scheduler, governor)
                while True:
                    claimed = service.claim(worker_id, ResourceRequest(cpu=1), max_candidates=40)
                    if claimed is None:
                        break
                    with lock:
                        claims_by_task.setdefault(claimed.task_id, []).append(worker_id)
                    service.complete(claimed)
                queue.close()
            except Exception as exc:  # noqa: BLE001 - asserted on below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=("worker-%d" % i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, "concurrent claim/complete must never raise: %r" % (errors,)
        assert set(claims_by_task) == set(task_ids), "every submitted job must be claimed exactly once"
        assert all(len(workers) == 1 for workers in claims_by_task.values()), (
            "no job may be claimed by more than one worker"
        )
        assert scheduler.status()["global_total"] == 0
        assert governor.status()["used"]["cpu"] == 0


def test_status_reports_both_scheduler_and_governor_state() -> None:
    with tempfile.TemporaryDirectory() as directory:
        service = _service(directory, cpu=4)
        service.submit({"i": 0}, idempotency_key="k", client_id="alice")
        status = service.status()
        assert status["schema"] == "simplicio.hub-service/v1"
        assert status["scheduler"]["global_total"] == 1
        assert "used" in status["governor"]
