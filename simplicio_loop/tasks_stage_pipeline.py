"""Concrete stage/review/delivery coordinator for dispatched task items."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .stage_agent_coordinator import CommandAgentAdapter, StageAgentCoordinator, StageCoordinatorJournal

_SENSITIVE_KEYS = frozenset({"authorization", "token", "password", "secret", "api_key", "access_token"})

def _receipt_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

def _git_result(argv: Sequence[str]) -> subprocess.CompletedProcess:
    if os.name == "nt":
        with tempfile.TemporaryDirectory(prefix="simplicio-tasks-git-") as directory:
            stdout_path = Path(directory) / "stdout.txt"
            stderr_path = Path(directory) / "stderr.txt"
            result = None
            for attempt in range(5):
                try:
                    with stdout_path.open("w", encoding="utf-8") as stdout_handle, \
                            stderr_path.open("w", encoding="utf-8") as stderr_handle:
                        result = subprocess.run(list(argv), shell=False, stdout=stdout_handle,
                                                stderr=stderr_handle, stdin=subprocess.DEVNULL,
                                                close_fds=False, text=True,
                                                timeout=10, check=False)
                    break
                except OSError as exc:
                    if getattr(exc, "winerror", None) not in {6, 50}:
                        return subprocess.CompletedProcess(list(argv), 1, "", str(exc))
                    if attempt < 4:
                        time.sleep(0.25)
            if result is None:
                return subprocess.CompletedProcess(list(argv), 1, "", "git capture unavailable")
            return subprocess.CompletedProcess(
                list(argv), result.returncode,
                stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else "",
                stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else "",
            )
    last = None
    for attempt in range(5):
        try:
            result = subprocess.run(list(argv), capture_output=True, text=True, timeout=10, check=False)
            last = result
            if result.returncode == 0:
                return result
        except OSError as exc:
            last = exc
            if getattr(exc, "winerror", None) != 6:
                raise
        if attempt < 4:
            time.sleep(0.25)
    if isinstance(last, subprocess.CompletedProcess):
        return last
    raise last  # type: ignore[misc]

def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): ("[REDACTED]" if str(key).lower() in _SENSITIVE_KEYS else _redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value

def _git_merge_authentic(worktree: str, head_sha: str, merge_sha: str) -> bool:
    if not worktree or not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
        return False
    try:
        if os.name == "nt":
            time.sleep(0.5)
        resolved = _git_result(["git", "-C", worktree, "rev-parse", f"{merge_sha}^2"])
    except (OSError, subprocess.SubprocessError):
        return False
    return resolved.returncode == 0 and resolved.stdout.strip() == head_sha

def _coordinator_merge_receipt(delivery: Mapping[str, Any], worker: Mapping[str, Any]) -> Mapping[str, Any] | None:
    candidate = delivery.get("merge_receipt") if isinstance(delivery.get("merge_receipt"), Mapping) else {}
    merge_sha = str(candidate.get("merge_commit_sha") or "")
    worktree = str(worker.get("worktree_path") or worker.get("repo") or "")
    head_ref = str(worker.get("branch") or worker.get("expected_pr_head") or "")
    base_ref = str(worker.get("expected_base_ref") or "")
    expected_base_sha = str(worker.get("expected_base_sha") or "")
    if (not worktree or not head_ref or not base_ref
            or not re.fullmatch(r"[0-9a-fA-F]{40}", expected_base_sha)
            or not re.fullmatch(r"[0-9a-fA-F]{40}", merge_sha)):
        return None
    try:
        def resolve(ref):
            result = _git_result(["git", "-C", worktree, "rev-parse", "--verify", ref])
            return result.stdout.strip() if result.returncode == 0 else ""
        parent1, parent2 = resolve(f"{merge_sha}^1"), resolve(f"{merge_sha}^2")
        base_sha, head_sha = expected_base_sha, resolve(head_ref)
    except (OSError, subprocess.SubprocessError):
        return None
    if not all((parent1, parent2, base_sha, head_sha)) or parent1 != base_sha or parent2 != head_sha:
        return None
    authority = worker.get("authority_receipt") if isinstance(worker.get("authority_receipt"), Mapping) else {}
    authority_hash = str(authority.get("receipt_hash") or "")
    if not authority_hash:
        return None
    receipt = {"schema": "simplicio.tasks-merge-receipt/v1", "issuer": "tasks-coordinator",
               "merged": True, "pr_url": str(delivery.get("pr_url") or ""),
               "repo": str(delivery.get("pr_repo") or ""), "base_ref": base_ref,
               "pr_head": head_ref, "base_sha": base_sha, "head_sha": head_sha,
               "merge_commit_sha": merge_sha, "merge_parents": [parent1, parent2],
               "admission_fence": int(worker.get("admission_fence") or 0),
               "authority_receipt_hash": authority_hash}
    receipt["receipt_sha"] = _receipt_digest(receipt)
    return receipt

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
    merge = _coordinator_merge_receipt(delivery, worker)
    if delivery.get("operation") != "merge" or merge is None:
        required.append("merge_receipt_invalid")
    expected_repo = str(worker.get("expected_pr_repo") or "")
    expected_head = str(worker.get("branch") or worker.get("expected_pr_head") or "")
    expected_source = str(worker.get("source_issue") or worker.get("task_id") or "").removeprefix("issue-")
    expected_fence = int(worker.get("admission_fence") or 1)
    if int(delivery.get("admission_fence") or 0) != expected_fence:
        required.append("admission_fence_mismatch")
    worktree = str(worker.get("worktree_path") or worker.get("repo") or "")
    if merge is None or not _git_merge_authentic(worktree, str(merge.get("head_sha") or ""), str(merge.get("merge_commit_sha") or "")):
        required.append("merge_not_locally_verified")
    if expected_repo and str(delivery.get("pr_repo")) != expected_repo:
        required.append("pr_repo_mismatch")
    if expected_head and str(delivery.get("pr_head")) != expected_head:
        required.append("pr_head_mismatch")
    if expected_source and str(delivery.get("source_issue")).removeprefix("#") != expected_source.removeprefix("#"):
        required.append("source_issue_mismatch")
    authoritative = dict(delivery)
    authoritative["merge_receipt"] = merge
    return authoritative, sorted(set(required))

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
            safe = re.compile(r"^[A-Za-z0-9_.-]+$")
            if not safe.fullmatch(run_id) or not safe.fullmatch(task_id):
                evidence.append({"task_id": task_id, "pr": None, "verification": None,
                                 "delivery_errors": ["unsafe_journal_identity"], "receipts": [], "status": {}})
                all_passed = False
                continue
            journal_path = (self.journal_dir / f"{run_id}-{task_id}.jsonl").resolve()
            if self.journal_dir != journal_path.parent:
                raise ValueError("journal path escapes tasks journal root")
            journal = StageCoordinatorJournal(journal_path)
            worktree = str(worker.get("worktree_path") or worker.get("repo") or Path.cwd())
            adapter = CommandAgentAdapter(command=self.command, cwd=worktree, extra_env={
                "SIMPLICIO_TASK_WORKTREE": worktree,
                "SIMPLICIO_TASK_BRANCH": str(worker.get("branch") or ""),
                "SIMPLICIO_TASK_HEAD": str(worker.get("head_sha") or ""),
                "SIMPLICIO_ADMISSION_FENCE": str(int(worker.get("admission_fence") or 1)),
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
