"""Deterministic GitHub pull-request patrol for Simplicio-loop delivery waves.

The loop does not wait until the end of a backlog to discover review feedback or
merge conflicts.  A patrol is due after every two completed work items, before
the final completion claim, and immediately after a successful merge.  It is a
read-only projection: it never merges, closes, or rewrites a pull request.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping


SCHEMA = "simplicio.pr-patrol/v1"
DEFAULT_CADENCE = 2
Runner = Callable[..., "subprocess.CompletedProcess[str]"]

_ACTIONABLE = {
    "CONFLICTING",
    "REBASE_REQUIRED",
    "REVIEW_CHANGES_REQUESTED",
    "REVIEW_REQUIRED",
    "CHECKS_FAILED",
}
_FAILED_CHECKS = {"ACTION_REQUIRED", "CANCELLED", "FAILURE", "STALE", "TIMED_OUT"}
_PENDING_CHECKS = {"", "IN_PROGRESS", "NEUTRAL", "PENDING", "QUEUED", "SKIPPED", "STARTUP_FAILURE"}


class PrPatrolError(RuntimeError):
    """The GitHub query could not be completed; no readiness is inferred."""


def patrol_due(completed_items: int, *, cadence: int = DEFAULT_CADENCE,
               final: bool = False, post_merge: bool = False) -> Dict[str, Any]:
    """Return the deterministic cadence decision without performing network I/O."""
    if cadence < 1:
        raise ValueError("cadence must be at least 1")
    completed = max(0, int(completed_items))
    if post_merge:
        return {"due": True, "reason": "post_merge", "cadence": cadence, "completed_items": completed}
    if final:
        return {"due": True, "reason": "final_reconciliation", "cadence": cadence,
                "completed_items": completed}
    return {
        "due": completed > 0 and completed % cadence == 0,
        "reason": "cadence" if completed > 0 and completed % cadence == 0 else "not_due",
        "cadence": cadence,
        "completed_items": completed,
    }


def _check_signals(checks: Iterable[Any]) -> List[str]:
    signals: List[str] = []
    for check in checks or []:
        if not isinstance(check, Mapping):
            continue
        conclusion = str(check.get("conclusion") or "").upper()
        status = str(check.get("status") or "").upper()
        value = conclusion or status
        if value in _FAILED_CHECKS:
            signals.append("CHECKS_FAILED")
        elif value in _PENDING_CHECKS:
            signals.append("CHECKS_PENDING")
    return signals


def classify_pr(pr: Mapping[str, Any]) -> Dict[str, Any]:
    """Classify one open PR into repair/review actions without making mutations."""
    signals: List[str] = []
    mergeable = str(pr.get("mergeable") or "").upper()
    merge_state = str(pr.get("mergeStateStatus") or "").upper()
    review = str(pr.get("reviewDecision") or "").upper()
    if bool(pr.get("isDraft")):
        signals.append("DRAFT")
    if mergeable == "CONFLICTING" or merge_state == "DIRTY":
        signals.append("CONFLICTING")
    elif merge_state == "BEHIND":
        signals.append("REBASE_REQUIRED")
    if review == "CHANGES_REQUESTED":
        signals.append("REVIEW_CHANGES_REQUESTED")
    elif review == "REVIEW_REQUIRED":
        signals.append("REVIEW_REQUIRED")
    signals.extend(_check_signals(pr.get("statusCheckRollup") or []))
    # Keep signal order stable and prevent a malformed API response from duplicating work.
    signals = list(dict.fromkeys(signals))
    return {
        "number": int(pr.get("number") or 0),
        "url": str(pr.get("url") or ""),
        "head": str(pr.get("headRefName") or ""),
        "base": str(pr.get("baseRefName") or ""),
        "signals": signals,
        "action_required": any(signal in _ACTIONABLE for signal in signals),
    }


@dataclass
class PrPatrol:
    repo: str
    runner: Runner = subprocess.run
    timeout: int = 30

    def __post_init__(self) -> None:
        if not str(self.repo).strip():
            raise ValueError("repo is required (owner/name)")
        self.repo = str(self.repo).strip()

    def inspect(self, *, completed_items: int = 0, cadence: int = DEFAULT_CADENCE,
                final: bool = False, post_merge: bool = False) -> Dict[str, Any]:
        decision = patrol_due(completed_items, cadence=cadence, final=final, post_merge=post_merge)
        report: Dict[str, Any] = {"schema": SCHEMA, "repo": self.repo, **decision,
                                  "open_prs": [], "action_required": [], "clean": []}
        if not decision["due"]:
            return report
        completed = self.runner([
            "gh", "pr", "list", "--repo", self.repo, "--state", "open",
            "--json", "number,url,headRefName,baseRefName,isDraft,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup",
        ], capture_output=True, text=True, timeout=self.timeout)
        if completed.returncode != 0:
            raise PrPatrolError((completed.stderr or completed.stdout or "gh pr list failed").strip())
        try:
            rows = json.loads(completed.stdout or "[]")
        except (TypeError, ValueError) as exc:
            raise PrPatrolError("gh pr list returned invalid JSON") from exc
        if not isinstance(rows, list):
            raise PrPatrolError("gh pr list returned a non-list payload")
        report["open_prs"] = [classify_pr(row) for row in rows if isinstance(row, Mapping)]
        report["action_required"] = [row for row in report["open_prs"] if row["action_required"]]
        report["clean"] = [row["number"] for row in report["open_prs"] if not row["action_required"]]
        return report


__all__ = ["DEFAULT_CADENCE", "PrPatrol", "PrPatrolError", "SCHEMA", "classify_pr", "patrol_due"]
