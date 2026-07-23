"""Unit-level validation and fail-closed delivery coverage for worker state."""

import tempfile
from pathlib import Path

import pytest

from simplicio_loop.hub_worker_store import HubWorkerError, HubWorkerStore


def _payload(key="key"):
    return {
        "schema": "simplicio.code-worker-adapter/v1",
        "protocol": "simplicio.loop-worker/v1",
        "identity": {
            "coordinator_id": "c", "session_id": "s", "turn_id": "t",
            "run_id": "r", "goal_id": "g",
        },
        "idempotency_key": key,
        "max_concurrency": 1,
        "tasks": [{"task_id": "task", "role": "implementer", "depends_on": [], "task_contract": "contract"}],
    }


def test_worker_store_validates_contracts_and_requires_remote_delivery_confirmation():
    with tempfile.TemporaryDirectory() as directory:
        store = HubWorkerStore(str(Path(directory) / "workers.db"))
        with pytest.raises(HubWorkerError):
            store.delegate({**_payload(), "schema": "wrong/v1"})
        with pytest.raises(HubWorkerError):
            store.delegate({**_payload(), "identity": None})
        with pytest.raises(HubWorkerError):
            store.delegate({**_payload(), "max_concurrency": 0})
        with pytest.raises(HubWorkerError):
            store.delegate({**_payload(), "tasks": []})
        with pytest.raises(HubWorkerError):
            store.delegate({**_payload(), "tasks": [{"task_id": "task", "role": "bogus", "depends_on": [], "task_contract": "x"}]})
        with pytest.raises(HubWorkerError):
            store.delegate({**_payload(), "tasks": [{"task_id": "task", "role": "implementer", "depends_on": ["missing"], "task_contract": "x"}]})
        with pytest.raises(HubWorkerError):
            store.delegate({**_payload(), "tasks": [
                {"task_id": "a", "role": "implementer", "depends_on": ["b"], "task_contract": "x"},
                {"task_id": "b", "role": "reviewer", "depends_on": ["a"], "task_contract": "x"},
            ]})

        receipt = store.delegate(_payload())
        with pytest.raises(HubWorkerError, match="conflicting"):
            store.delegate({**_payload(), "tasks": [{"task_id": "task", "role": "implementer", "depends_on": [], "task_contract": "changed"}]})
        with pytest.raises(HubWorkerError):
            store.status({"workflow_id": "missing", "after_sequence": 0})
        with pytest.raises(HubWorkerError):
            store.status({"workflow_id": receipt["workflow_id"], "after_sequence": -1})
        with pytest.raises(HubWorkerError):
            store.cancel({"workflow_id": receipt["workflow_id"], "idempotency_key": "cancel", "reason": "stop", "revoke_mutation_authority": False})
        cancelled = store.cancel({"workflow_id": receipt["workflow_id"], "idempotency_key": "cancel", "reason": "stop", "revoke_mutation_authority": True})
        assert store.cancel({"workflow_id": receipt["workflow_id"], "idempotency_key": "cancel", "reason": "stop", "revoke_mutation_authority": True}) == cancelled
        with pytest.raises(HubWorkerError, match="revoked"):
            store.deliver({"workflow_id": receipt["workflow_id"], "task_id": "task", "agent_id": "a", "review_receipt_id": "review"})
        store.close()

        delivery_store = HubWorkerStore(str(Path(directory) / "delivery.db"))
        delivery = _payload("delivery")
        delivery["tasks"][0]["role"] = "delivery"
        delivery_receipt = delivery_store.delegate(delivery)
        with pytest.raises(HubWorkerError, match="delivery requires"):
            delivery_store.deliver({"workflow_id": delivery_receipt["workflow_id"], "task_id": "task", "agent_id": "external-agent:task", "review_receipt_id": "review"})
        with delivery_store._db:
            delivery_store._db.execute(
                "UPDATE worker_tasks SET state='done' WHERE workflow_id=? AND task_id='task'",
                (delivery_receipt["workflow_id"],),
            )
        unconfirmed = delivery_store.deliver({"workflow_id": delivery_receipt["workflow_id"], "task_id": "task", "agent_id": "external-agent:task", "review_receipt_id": "review"})
        assert unconfirmed["remotely_confirmed"] is False
        delivery_store.close()
