"""MapperStore-backed agent-slot lifecycle for the explicit Loop route."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .mapper_operations import MapperOperationsAdapter


SCHEMA = "simplicio.loop-agent-slots/v1"


class MapperAgentSlotRegistry:
    """Agent-slot facade that never creates a local SQLite authority."""

    def __init__(
        self,
        database: str | Path,
        *,
        capacity: int = 6,
        auto_create: bool = False,
        adapter: MapperOperationsAdapter | None = None,
    ) -> None:
        self.capacity = capacity
        self._adapter = adapter or MapperOperationsAdapter(database, auto_create=auto_create)

    def initialize(self) -> dict[str, Any]:
        result = self._adapter.initialize()
        self._adapter.configure_agent_slots(self.capacity)
        return result

    def status(self) -> dict[str, Any]:
        return self._adapter.agent_slot_status()

    def acquire(self, agent_id: str, *, worktree: str | None = None, lease_id: str | None = None) -> dict[str, Any]:
        return self._adapter.agent_slot_acquire(agent_id, worktree=worktree, lease_id=lease_id)

    def start(self, agent_id: str) -> dict[str, Any]:
        return self._adapter.agent_slot_transition(agent_id, "running", reason="slot_started")

    def close_agent(self, agent_id: str, *, status: str = "completed", reason: str = "") -> dict[str, Any]:
        if status not in ("completed", "shutdown"):
            raise ValueError("close status must be completed or shutdown")
        return self._adapter.agent_slot_transition(agent_id, status, reason=reason or "agent_terminal")

    def update_blockers(
        self,
        agent_id: str,
        *,
        descendants: int = 0,
        worktree_active: bool = False,
        lease_active: bool = False,
    ) -> dict[str, Any]:
        return self._adapter.agent_slot_update_blockers(
            agent_id,
            descendants=descendants,
            worktree_active=worktree_active,
            lease_active=lease_active,
        )

    def reclaim(self, agent_id: str | None = None) -> dict[str, Any]:
        return self._adapter.agent_slot_reclaim(agent_id)


__all__ = ["MapperAgentSlotRegistry", "SCHEMA"]
