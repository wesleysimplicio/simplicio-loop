from __future__ import annotations

from simplicio_loop.mapper_agent_slots import MapperAgentSlotRegistry


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def initialize(self):
        self.calls.append(("initialize",))
        return {"status": "ready"}

    def configure_agent_slots(self, capacity):
        self.calls.append(("configure", capacity))
        return {"capacity": capacity}

    def agent_slot_status(self):
        self.calls.append(("status",))
        return {"active_slots": 0}

    def agent_slot_acquire(self, agent_id, **kwargs):
        self.calls.append(("acquire", agent_id, kwargs))
        return {"accepted": True}

    def agent_slot_transition(self, agent_id, target, **kwargs):
        self.calls.append(("transition", agent_id, target, kwargs))
        return {"accepted": True}

    def agent_slot_update_blockers(self, agent_id, **kwargs):
        self.calls.append(("blockers", agent_id, kwargs))
        return {"accepted": True}

    def agent_slot_reclaim(self, agent_id=None):
        self.calls.append(("reclaim", agent_id))
        return {"accepted": True}


def test_mapper_registry_is_a_thin_lifecycle_facade():
    adapter = FakeAdapter()
    registry = MapperAgentSlotRegistry("/tmp/operations.sqlite", capacity=3, adapter=adapter)
    registry.initialize()
    registry.acquire("agent-a", worktree="wt", lease_id="lease")
    registry.start("agent-a")
    registry.update_blockers("agent-a", lease_active=True)
    registry.close_agent("agent-a", status="shutdown", reason="test")
    registry.reclaim("agent-a")
    assert adapter.calls == [
        ("initialize",),
        ("configure", 3),
        ("acquire", "agent-a", {"worktree": "wt", "lease_id": "lease"}),
        ("transition", "agent-a", "running", {"reason": "slot_started"}),
        ("blockers", "agent-a", {"descendants": 0, "worktree_active": False, "lease_active": True}),
        ("transition", "agent-a", "shutdown", {"reason": "test"}),
        ("reclaim", "agent-a"),
    ]
