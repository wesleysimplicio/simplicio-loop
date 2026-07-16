"""Best-effort Orca Dev worktree-card lifecycle projection.

Orca is a host rather than a second source-of-truth API.  The adapter therefore
uses the public ``orca worktree`` CLI and scopes every write to the active
worktree only after ``worktree current`` proves that an Orca context exists.
Outside Orca this is an explicit no-op, so a local or GitHub-only run cannot
touch another workspace's card.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Callable, Dict, Mapping, Sequence


ORCA_STATUS_BY_LIFECYCLE = {
    "DISCOVERED": "todo",
    "CLAIMED": "in-progress",
    "PLANNED": "in-progress",
    "IN_PROGRESS": "in-progress",
    "VERIFYING": "in-progress",
    "BLOCKED": "in-progress",
    "PAUSED_NETWORK": "in-progress",
    "AWAITING_DECISION": "in-progress",
    "PR_OPEN": "in-review",
    "MERGE_READY": "in-review",
    "MERGED": "completed",
    "CLOSING": "completed",
    "CLOSE_PENDING_RECONCILIATION": "in-progress",
    "CLOSED": "completed",
    "RELEASED": "completed",
}


def lifecycle_to_orca_status(state: str) -> str:
    return ORCA_STATUS_BY_LIFECYCLE.get(str(state or "").upper(), "in-progress")


def _disabled() -> bool:
    return str(os.environ.get("SIMPLICIO_LOOP_ORCA_LIFECYCLE_SYNC") or "").strip().lower() in {
        "0", "false", "no", "off", "legacy",
    }


def _command() -> str:
    return str(os.environ.get("SIMPLICIO_LOOP_ORCA_COMMAND") or "orca").strip() or "orca"


def _run_default(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([_command(), *args], capture_output=True, text=True, timeout=20)


def sync_orca_status(state: Mapping[str, Any], event: Mapping[str, Any], *,
                     runner: Callable[..., subprocess.CompletedProcess[str]] = _run_default) -> Dict[str, Any]:
    """Update only the current Orca worktree card, or return an explicit skip."""
    if _disabled():
        return {"status": "skipped", "reason": "disabled"}
    current = runner(["worktree", "current", "--json"])
    if current.returncode != 0:
        return {"status": "skipped", "reason": "not_in_orca", "detail": (current.stderr or "").strip()[:240]}
    try:
        context = json.loads(current.stdout or "{}")
    except (TypeError, ValueError):
        return {"status": "skipped", "reason": "invalid_orca_context"}
    if not isinstance(context, dict) or not context.get("id"):
        return {"status": "skipped", "reason": "no_active_worktree"}

    lifecycle = str(event.get("lifecycle_state") or event.get("state") or state.get("phase") or "IN_PROGRESS").upper()
    status = lifecycle_to_orca_status(lifecycle)
    run_id = str(state.get("run_id") or "")
    message = str(event.get("message") or event.get("reason") or "").strip().replace("\n", " ")
    comment = f"Simplicio Loop — {lifecycle}"
    if run_id:
        comment += f" · run {run_id}"
    if message:
        comment += f" · {message[:180]}"
    updated = runner([
        "worktree", "set", "--worktree", "active", "--comment", comment,
        "--workspace-status", status, "--json",
    ])
    if updated.returncode != 0:
        return {"status": "failed", "reason": "orca_update_failed", "detail": (updated.stderr or "").strip()[:240]}
    return {
        "status": "synced", "worktree_id": str(context.get("id")),
        "lifecycle_state": lifecycle, "workspace_status": status, "comment": comment,
    }
