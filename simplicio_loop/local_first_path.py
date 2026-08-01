"""One bounded local-first task path across Mapper receipt, Fast and Dev CLI."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .fast_integration import FastLoopIntegration
from .fast_task_bridge import FastTaskBinding, FastTaskBridge

SCHEMA = "simplicio.loop.local-first-task-path/v1"


class LocalFirstPathError(RuntimeError):
    """The standalone local path cannot safely continue."""


@dataclass(frozen=True)
class LocalFirstTaskResult:
    task_id: str
    status: str
    binding: FastTaskBinding
    plan_receipt: Mapping[str, Any]
    apply_receipt: Mapping[str, Any] | None = None
    schema: str = SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "task_id": self.task_id, "status": self.status,
                "binding": self.binding.to_dict(), "plan_receipt": dict(self.plan_receipt),
                "apply_receipt": dict(self.apply_receipt or {})}


class LocalFirstTaskPath:
    """Orchestrate the existing Fast integration and public workspace binding."""

    def __init__(self, root: str, *, bridge: FastTaskBridge | None = None,
                 integration: FastLoopIntegration | None = None) -> None:
        self.bridge = bridge or FastTaskBridge(root)
        self.integration = integration or FastLoopIntegration(root)

    def run(self, *, task_id: str, attempt_id: str, worktree_id: str, task: str,
            mapper_receipt: Mapping[str, Any], changeset: Mapping[str, Any] | None = None
            ) -> LocalFirstTaskResult:
        binding = self.bridge.prepare(task_id=task_id, attempt_id=attempt_id,
                                      worktree_id=worktree_id, mapper_receipt=mapper_receipt)
        try:
            plan = self.integration.prepare(task)
            if plan.get("status") != "READY":
                raise LocalFirstPathError(f"Fast plan did not become READY: {plan.get('reason', 'unknown')}")
            if changeset is None:
                return LocalFirstTaskResult(task_id, "PLANNED", binding, plan)
            candidate = self.bridge.validate_changeset(binding, changeset)
            applied = self.integration.apply(candidate, winner=True,
                                              generation=binding.overlay_generation,
                                              context_hash=binding.mapper_context_hash)
            if str(applied.get("status") or "").upper() not in {"APPLIED", "READY", "MEASURED"}:
                raise LocalFirstPathError(f"Fast apply was not accepted: {applied.get('status', 'unknown')}")
            result = LocalFirstTaskResult(task_id, "APPLIED", binding, plan, applied)
            self.bridge.release(binding)
            return result
        except Exception:
            self.bridge.release(binding)
            raise


__all__ = ["SCHEMA", "LocalFirstPathError", "LocalFirstTaskPath", "LocalFirstTaskResult"]
