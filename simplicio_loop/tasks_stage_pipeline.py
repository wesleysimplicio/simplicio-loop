"""Concrete stage/review/delivery coordinator for dispatched task items."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .stage_agent_coordinator import CommandAgentAdapter, StageAgentCoordinator, StageCoordinatorJournal

_SENSITIVE_KEYS = frozenset({"authorization", "token", "password", "secret", "api_key", "access_token"})

def _receipt_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): ("[REDACTED]" if str(key).lower() in _SENSITIVE_KEYS else _redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value

def _delivery_receipt(values: Sequence[Any], worker: Mapping[str, Any]) -> tuple[Mapping[str, Any] | None, list[str]]:
    delivery = next((value for value in values if isinstance(value, Mapping) and value.get("pr_url")), None)
    if delivery is None:
        return None, ["pr_url_missing"]
    required = [name for name in ("pr_repo", "pr_head", "source_issue", "checks") if not delivery.get(name)]
    checks = delivery.get("checks") or []
    if (not isinstance(checks, list) or not checks
            or any(not isinstance(check, Mapping)
                   or not str(check.get("name") or "").strip()
                   or str(check.get("conclusion") or "").upper() != "SUCCESS"
                   for check in checks)):
        required.append("checks_not_successful")
    repo = str(delivery.get("pr_repo") or "")
    url = str(delivery.get("pr_url") or "")
    if not repo or not re.fullmatch(rf"https://github\.com/{re.escape(repo)}/pull/[1-9][0-9]*", url):
        required.append("pr_url_mismatch")
    merge = delivery.get("merge_receipt")
    merge_payload = dict(merge) if isinstance(merge, Mapping) else {}
    merge_digest = str(merge_payload.pop("receipt_sha", ""))
    if (delivery.get("operation") != "merge" or not isinstance(merge, Mapping)
            or merge.get("schema") != "simplicio.tasks-merge-receipt/v1"
            or merge.get("merged") is not True
            or str(merge.get("pr_url") or "") != url
            or not re.fullmatch(r"[0-9a-fA-F]{40}", str(merge.get("merge_commit_sha") or ""))
            or not merge_digest or merge_digest != _receipt_digest(merge_payload)):
        required.append("merge_receipt_invalid")
    expected_repo = str(worker.get("expected_pr_repo") or "")
    expected_head = str(worker.get("branch") or worker.get("expected_pr_head") or "")
    expected_source = str(worker.get("source_issue") or worker.get("task_id") or "").removeprefix("issue-")
    if expected_repo and str(delivery.get("pr_repo")) != expected_repo:
        required.append("pr_repo_mismatch")
    if expected_head and str(delivery.get("pr_head")) != expected_head:
        required.append("pr_head_mismatch")
    if expected_source and str(delivery.get("source_issue")).removeprefix("#") != expected_source.removeprefix("#"):
        required.append("source_issue_mismatch")
    return delivery, sorted(set(required))

class CommandPipelineCoordinator:
    def __init__(self, command: Sequence[str], journal_dir: str, *, host_total_slots: int = 4, coordinator_factory: Callable[..., Any] = StageAgentCoordinator):
        if not command:
            raise ValueError("agent command is required")
        self.command = list(command)
        self.journal_dir = Path(journal_dir).resolve()
        self.host_total_slots = host_total_slots
        self.coordinator_factory = coordinator_factory
        self.active = []
        self.cancel_path = self.journal_dir / "cancel.json"

    def cancel_all(self, *, reason: str) -> list[str]:
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        self.cancel_path.write_text(json.dumps({"schema": "simplicio.tasks-cancel/v1", "reason": reason}, sort_keys=True), encoding="utf-8")
        cancelled = []
        for coordinator in list(self.active):
            cancelled.extend(coordinator.cancel_all(reason=reason))
        return cancelled
    def __call__(self, dispatched: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.cancel_path.exists():
            cancel = json.loads(self.cancel_path.read_text(encoding="utf-8"))
            return {"passed": False, "cancelled": True, "reason": cancel.get("reason", "cancel_requested"), "evidence": []}
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
            try:
                results = coordinator.run_all()
                passed = bool(results) and all(result.status == "passed" for result in results.values()) and coordinator.terminal_reached()
                receipts = [_redact(result.instance.receipt) for result in results.values() if result.instance and result.instance.receipt]
                outputs = [_redact(result.instance.output) for result in results.values() if result.instance and result.instance.output]
                delivery, delivery_errors = _delivery_receipt([*outputs, *receipts], worker)
                pr_url = str(delivery.get("pr_url")) if delivery else ""
                verified = passed and not delivery_errors
                all_passed = all_passed and verified
                evidence.append({"task_id": task_id, "pr": pr_url or None, "verification": "passed" if verified else None, "delivery_errors": delivery_errors, "receipts": receipts, "status": coordinator.status_report()})
            finally:
                self.active.remove(coordinator)
        return {"passed": bool(evidence) and all_passed, "evidence": evidence}
