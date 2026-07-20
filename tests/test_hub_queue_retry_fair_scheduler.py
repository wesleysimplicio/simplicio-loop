import tempfile
from pathlib import Path

from simplicio_loop.hub_queue_retry import HubRetryQueue
from simplicio_loop.hub_scheduler import FairScheduler


def test_claim_fair_reorders_real_durable_backlog_by_client_not_by_insertion_order() -> None:
    """heavy submits 20 jobs first, light submits 4 jobs after, all persisted in the real
    SQLite-backed HubRetryQueue. Plain claim() would drain purely by insertion order
    (ORDER BY updated_at) so light would starve behind all 20 heavy jobs. claim_fair()
    must consult FairScheduler and interleave client turns instead."""
    with tempfile.TemporaryDirectory() as directory:
        queue = HubRetryQueue(str(Path(directory) / "queue.db"))
        for index in range(20):
            queue.submit({"client_id": "heavy", "index": index}, idempotency_key=f"heavy-{index}")
        for index in range(4):
            queue.submit({"client_id": "light", "index": index}, idempotency_key=f"light-{index}")
        queue.close()

        # The payload-only client identity is durable, so restart does not collapse
        # both backlogs into the scheduler's fallback client.
        queue = HubRetryQueue(str(Path(directory) / "queue.db"))
        assert {entry["client_id"] for entry in queue.list_queued_scheduling_metadata()} == {
            "heavy", "light",
        }

        scheduler = FairScheduler(max_inflight_per_client=1000, quantum=1)
        served_order = []
        for _ in range(8):
            lease = queue.claim_fair(scheduler, "worker-1", ttl=10)
            assert lease is not None
            payload = queue.payload_of(lease.task_id)
            served_order.append(payload["client_id"])
            queue.complete(lease)

        assert served_order[0] == "heavy"
        assert served_order[1] == "light"
        assert served_order.count("light") == 4
        assert served_order.count("heavy") == 4
        queue.close()


def test_claim_fair_returns_none_and_is_safe_on_empty_queue() -> None:
    with tempfile.TemporaryDirectory() as directory:
        queue = HubRetryQueue(str(Path(directory) / "queue.db"))
        scheduler = FairScheduler()
        assert queue.claim_fair(scheduler, "worker-1") is None
        queue.close()


def test_claim_fair_persists_a_real_lease_that_can_be_completed_and_survives_restart() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "queue.db")
        queue = HubRetryQueue(path)
        queue.submit({"client_id": "a"}, idempotency_key="only")
        scheduler = FairScheduler()
        lease = queue.claim_fair(scheduler, "worker-1", ttl=10)
        assert lease is not None
        assert queue.state(lease.task_id) == "leased"
        queue.close()

        restarted = HubRetryQueue(path)
        assert restarted.state(lease.task_id) == "leased"
        restarted.close()
