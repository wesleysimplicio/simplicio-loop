"""Concrete stage/review/delivery coordinator for dispatched task items."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .stage_agent_coordinator import CommandAgentAdapter, StageAgentCoordinator, StageCoordinatorJournal

_SENSITIVE_KEYS = frozenset({"authorization", "token", "password", "secret", "api_key", "access_token"})

def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): ("[REDACTED]" if str(key).lower() in _SENSITIVE_KEYS else _redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value

class CommandPipelineCoordinator:
    def __init__(self, command: Sequence[str], journal_dir: str, *, host_total_slots: int = 4, coordinator_factory: Callable[..., Any] = StageAgentCoordinator):
        if not command:
            raise ValueError("agent command is required")
        self.command = list(command)
        self.journal_dir = Path(journal_dir).resolve()
        self.host_total_slots = host_total_slots
        self.coordinator_factory = coordinator_factory
        self.active = []

    def cancel_all(self, *, reason: str) -> list[str]:
        cancelled = []
        for coordinator in self.active:
            cancelled.extend(coordinator.cancel_all(reason=reason))
        return cancelled

    def __call__(self, dispatched: Mapping[str, Any]) -> Mapping[str, Any]:
        evidence = []
        all_passed = True
        workers = dispatched.get("workers") or dispatched.get("completed") or []
        for index, worker in enumerate(workers, start=1):
            run_id = str(worker.get("run_id") or dispatched.get("run_id") or f"tasks-{index}")
            task_id = str(worker.get("task_id") or f"task-{index}")
            journal = StageCoordinatorJournal(self.journal_dir / f"{run_id}-{task_id}.jsonl")
            worktree = str(worker.get("worktree_path") or worker.get("repo") or Path.cwd())
            adapter = CommandAgentAdapter(command=self.command, cwd=worktree, extra_env={
                "SIMPLICIO_TASK_WORKTREE": worktree,
                "SIMPLICIO_TASK_BRANCH": str(worker.get("branch") or ""),
                "SIMPLICIO_TASK_HEAD": str(worker.get("head_sha") or ""),
            })
            coordinator = self.coordinator_factory(run_id=run_id, task_id=task_id, adapters=[adapter], journal=journal, host_total_slots=self.host_total_slots)
            self.active.append(coordinator)
            results = coordinator.run_all()
            passed = bool(results) and all(result.status == "passed" for result in results.values()) and coordinator.terminal_reached()
            receipts = [_redact(result.instance.receipt) for result in results.values() if result.instance and result.instance.receipt]
            outputs = [_redact(result.instance.output) for result in results.values() if result.instance and result.instance.output]
            pr_url = next((str(value.get("pr_url")) for value in [*outputs, *receipts] if isinstance(value, Mapping) and value.get("pr_url")), "")
            all_passed = all_passed and passed and bool(pr_url)
            evidence.append({"task_id": task_id, "pr": pr_url or None, "verification": "passed" if passed else None, "receipts": receipts, "status": coordinator.status_report()})
            self.active.remove(coordinator)
        return {"passed": bool(evidence) and all_passed, "evidence": evidence}
