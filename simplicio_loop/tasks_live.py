"""Live composition root for ``simplicio tasks run``."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Sequence

from .worktree_queue import TaskSpec, WorktreeQueue
from .github_drain_intake import GitHubDrainIntake, ReadOnlyLocalGitMap, parse_natural_drain_request
from .source_adapter import GitHubSourceAdapter
from .tasks_materializer import LoopRunContractMaterializer
from .tasks_orchestrator import TasksOrchestrator
from .tasks_stage_pipeline import CommandPipelineCoordinator

def _forbidden_publish(*args, **kwargs):
    raise RuntimeError("tasks intake is read-only")

def run_live(
    request: str,
    *,
    workspace: str,
    agent_command: Sequence[str],
    action_gate: bool,
    cancel: bool = False,
    checkpoint: str = "",
    max_workers: int = 1,
    retry_budget: int = 1,
    source_factory: Callable[..., Any] = GitHubSourceAdapter,
    intake_factory: Callable[..., Any] = GitHubDrainIntake,
    materializer_factory: Callable[..., Any] = LoopRunContractMaterializer,
    pipeline_factory: Callable[..., Any] = CommandPipelineCoordinator,
    orchestrator_factory: Callable[..., Any] = TasksOrchestrator,
    queue_factory: Callable[..., Any] = WorktreeQueue,
) -> dict[str, Any]:
    root = Path(workspace).resolve()
    batch = hashlib.sha256(request.encode("utf-8")).hexdigest()[:16]
    journal_dir = root / ".simplicio" / "tasks-run" / batch / "journals"
    if cancel:
        journal_dir.mkdir(parents=True, exist_ok=True)
        cancel_path = journal_dir / "cancel.json"
        cancel_path.write_text('{"reason":"cancel_requested","schema":"simplicio.tasks-cancel/v1"}', encoding="utf-8")
        return {
            "schema": "simplicio.tasks-orchestrator/v1",
            "plan": None,
            "idempotency_key": hashlib.sha256(request.encode("utf-8")).hexdigest(),
            "state": "cancelled",
            "reason": "cancel_requested",
            "cancelled": ["cancel_requested"],
            "evidence": [],
        }
    pipeline = pipeline_factory(agent_command, str(journal_dir), host_total_slots=max_workers + 1)
    intent = parse_natural_drain_request(request)
    checkpoint_path = checkpoint or str(root / ".simplicio" / "tasks-run" / batch / "intake.json")
    source = source_factory(intent.owner, intent.repo, publish_comment_fn=_forbidden_publish)
    intake = intake_factory(source=source, checkpoint=checkpoint_path, workspace=str(root), map_reader=ReadOnlyLocalGitMap())
    materializer = materializer_factory(str(root))
    holder = {}

    def contracts(plan):
        rows = materializer(plan)
        queue = queue_factory(repo_root=str(root), run_id=f"tasks-{batch}", state_path=str(root / ".simplicio" / "tasks-run" / batch / "worktree-queue.json"), worktree_root=str(root / ".simplicio" / "tasks-worktrees" / batch))
        specs = [TaskSpec(id=row["task_id"], goal=row["task_spec"]["goal"], files_affected=list(row["task_spec"]["files_affected"])) for row in rows]
        queue.register_tasks(specs)
        holder["orchestrator"].worktree_queue = queue
        return rows

    orchestrator = orchestrator_factory(intake, contracts, pipeline, max_workers=max_workers, retry_budget=retry_budget, journal_dir=str(journal_dir))
    holder["orchestrator"] = orchestrator
    return orchestrator.run(request, action_gate=action_gate, cancel=cancel)
