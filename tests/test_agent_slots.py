from __future__ import annotations

from pathlib import Path

import pytest

from simplicio_loop.agent_slots import AgentSlotRegistry, AgentSlotValidationError


class FakeAdapter:
    def __init__(self):
        self.calls = []
        self.records = {}

    def agent_slot_status(self):
        active = sum(value["status"] in {"pending", "running"} for value in self.records.values())
        return {"active_slots": active, "records": list(self.records.values())}

    def agent_slot_acquire(self, agent_id, **kwargs):
        if agent_id in self.records:
            return {"accepted": False, "reason_code": "duplicate_agent"}
        record = {"agent_id": agent_id, "status": "pending", "attempt": 1}
        self.records[agent_id] = record
        self.calls.append(("acquire", agent_id, kwargs))
        return {"accepted": True, **record}

    def agent_slot_transition(self, agent_id, target, **kwargs):
        self.calls.append(("transition", agent_id, target, kwargs))
        if agent_id not in self.records:
            return {"accepted": False, "reason_code": "unknown_agent"}
        self.records[agent_id]["status"] = target
        return {"accepted": True, **self.records[agent_id]}

    def agent_slot_update_blockers(self, agent_id, **kwargs):
        self.calls.append(("blockers", agent_id, kwargs))
        return {"accepted": True, "agent_id": agent_id, **kwargs}

    def agent_slot_reclaim(self, agent_id=None):
        self.calls.append(("reclaim", agent_id))
        if agent_id:
            self.records.pop(agent_id, None)
        return {"accepted": True, "reclaimed": [agent_id] if agent_id else []}


def registry(tmp_path: Path, capacity: int = 6, retry_limit: int = 1) -> AgentSlotRegistry:
    return AgentSlotRegistry(tmp_path / "operations.sqlite", capacity=capacity, retry_limit=retry_limit, adapter=FakeAdapter())


def test_compatibility_registry_delegates_lifecycle_to_mapper_adapter(tmp_path: Path) -> None:
    slots = registry(tmp_path)
    assert slots.acquire("agent-a")["accepted"]
    assert slots.start("agent-a")["accepted"]
    assert slots.update_blockers("agent-a", lease_active=True)["accepted"]
    assert slots.close_agent("agent-a", status="completed")["accepted"]
    assert slots.reclaim("agent-a")["accepted"]


def test_spawn_batch_retries_without_local_state_store(tmp_path: Path) -> None:
    slots = registry(tmp_path, retry_limit=1)
    calls = []

    def spawn(agent_id, record):
        calls.append((agent_id, record["attempt"]))
        return len(calls) == 2

    result = slots.spawn_batch(["retry-me"], spawn)
    assert result["results"][0]["success"] is True
    assert calls == [("retry-me", 1), ("retry-me", 2)]


def test_spawn_batch_records_bounded_failure(tmp_path: Path) -> None:
    slots = registry(tmp_path, retry_limit=1)
    result = slots.spawn_batch(["always-fails"], lambda _agent_id, _record: False)
    assert result["results"][0]["success"] is False
    assert result["results"][0]["reason_code"] == "spawn_failed_retry_exhausted"


def test_invalid_configuration_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(AgentSlotValidationError):
        registry(tmp_path, capacity=0)
    with pytest.raises(AgentSlotValidationError):
        registry(tmp_path, retry_limit=-1)
