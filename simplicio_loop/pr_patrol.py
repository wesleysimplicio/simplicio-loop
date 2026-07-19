"""Deterministic GitHub pull-request patrol for Simplicio-loop delivery waves.

The loop does not wait until the end of a backlog to discover review feedback or
merge conflicts.  A patrol is due after every two completed work items, before
the final completion claim, and immediately after a successful merge.  It is a
read-only projection: it never merges, closes, or rewrites a pull request.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional


SCHEMA = "simplicio.pr-patrol/v1"
DEFAULT_CADENCE = 2
Runner = Callable[..., "subprocess.CompletedProcess[str]"]
ACCEPTANCE_REVIEW_MARKER = "<!-- simplicio-loop:acceptance-review:v1 -->"
ACCEPTANCE_VERDICTS = {"ACCEPTED", "CHANGES_REQUESTED", "UNVERIFIED"}

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


def extract_acceptance_criteria(body: str) -> List[Dict[str, Any]]:
    """Extract the checked AC checklist emitted by ``pr_evidence.py`` from a PR body.

    The parser is intentionally conservative: free prose is not promoted to an
    acceptance criterion. A cross-agent reviewer may mark a PR UNVERIFIED when
    no explicit checklist exists, but never ACCEPTED from an inferred one.
    """
    heading = re.search(r"(?im)^#{2,6}\s+acceptance criteria[^\n]*\n", body or "")
    if not heading:
        return []
    section = (body or "")[heading.end():]
    section = re.split(r"(?im)^#{1,6}\s+", section, maxsplit=1)[0]
    criteria: List[Dict[str, Any]] = []
    for line in section.splitlines():
        match = re.match(r"^\s*-\s*\[([ xX~])\]\s+(.*\S)\s*$", line)
        if not match:
            continue
        raw = match.group(2)
        criteria.append({
            "text": raw,
            "checked": match.group(1).lower() == "x",
            "evidence_present": bool(re.search(r"(?i)(?:_evidence:_|\bevidence\s*:)", raw)),
        })
    return criteria


def assess_acceptance_criteria(body: str) -> Dict[str, Any]:
    """Return an evidence gate for an AC review; it never evaluates code itself."""
    criteria = extract_acceptance_criteria(body)
    verified = sum(1 for item in criteria if item["checked"] and item["evidence_present"])
    return {
        "criteria": criteria,
        "total": len(criteria),
        "verified": verified,
        "eligible_for_accepted": bool(criteria) and verified == len(criteria),
    }


def render_acceptance_review_comment(packet: Mapping[str, Any], verdict: str, *, note: str) -> str:
    """Render one review receipt. This is a comment, never a GitHub approval."""
    verdict = str(verdict or "").upper()
    if verdict not in ACCEPTANCE_VERDICTS:
        raise ValueError("verdict must be one of %s" % ", ".join(sorted(ACCEPTANCE_VERDICTS)))
    assessment = packet.get("acceptance") or {}
    if verdict == "ACCEPTED" and not assessment.get("eligible_for_accepted"):
        raise ValueError("ACCEPTED requires an explicit fully checked AC checklist with evidence")
    note = str(note or "").strip()
    if not note:
        raise ValueError("review note is required")
    pr = packet.get("pr") or {}
    lines = [
        ACCEPTANCE_REVIEW_MARKER,
        "## Simplicio acceptance-criteria review",
        "",
        "- PR: #%s at `%s`" % (pr.get("number", "?"), pr.get("head_sha", "unverified")),
        "- Verdict: **%s**" % verdict,
        "- Anchored criteria with evidence: %s/%s" % (assessment.get("verified", 0), assessment.get("total", 0)),
        "- Reviewer note: %s" % note,
        "",
        "This is an evidence-backed coordination receipt, not a substitute for a required human approval.",
    ]
    return "\n".join(lines) + "\n"


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

    def _gh(self, args: Iterable[str], *, input_text: Optional[str] = None) -> "subprocess.CompletedProcess[str]":
        completed = self.runner(["gh"] + list(args), capture_output=True, text=True,
                                timeout=self.timeout, input=input_text)
        if completed.returncode != 0:
            raise PrPatrolError((completed.stderr or completed.stdout or "gh command failed").strip())
        return completed

    def inspect(self, *, completed_items: int = 0, cadence: int = DEFAULT_CADENCE,
                final: bool = False, post_merge: bool = False) -> Dict[str, Any]:
        decision = patrol_due(completed_items, cadence=cadence, final=final, post_merge=post_merge)
        report: Dict[str, Any] = {"schema": SCHEMA, "repo": self.repo, **decision,
                                  "open_prs": [], "action_required": [], "clean": []}
        if not decision["due"]:
            return report
        completed = self._gh([
            "pr", "list", "--repo", self.repo, "--state", "open",
            "--json", "number,url,headRefName,baseRefName,isDraft,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup",
        ])
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

    def review_packet(self, pr_number: int) -> Dict[str, Any]:
        """Fetch the reviewable PR facts and its explicit AC evidence packet."""
        completed = self._gh([
            "pr", "view", str(pr_number), "--repo", self.repo, "--json",
            "number,url,title,body,headRefOid,baseRefOid,headRefName,baseRefName,isDraft,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup",
        ])
        try:
            pr = json.loads(completed.stdout or "{}")
        except (TypeError, ValueError) as exc:
            raise PrPatrolError("gh pr view returned invalid JSON") from exc
        if not isinstance(pr, Mapping) or not pr.get("number"):
            raise PrPatrolError("gh pr view returned no pull request")
        body = str(pr.get("body") or "")
        return {
            "schema": SCHEMA,
            "repo": self.repo,
            "pr": {
                "number": int(pr["number"]), "url": str(pr.get("url") or ""),
                "title": str(pr.get("title") or ""), "head_sha": str(pr.get("headRefOid") or ""),
                "base_sha": str(pr.get("baseRefOid") or ""),
            },
            "acceptance": assess_acceptance_criteria(body),
            "delivery": classify_pr(pr),
        }

    def _review_comment_id(self, pr_number: int) -> Optional[int]:
        completed = self._gh([
            "api", "repos/%s/issues/%s/comments" % (self.repo, pr_number), "--paginate",
        ])
        try:
            comments = json.loads(completed.stdout or "[]")
        except (TypeError, ValueError) as exc:
            raise PrPatrolError("GitHub returned invalid PR-comment JSON") from exc
        if not isinstance(comments, list):
            raise PrPatrolError("GitHub returned a non-list PR-comment payload")
        for comment in comments:
            if isinstance(comment, Mapping) and ACCEPTANCE_REVIEW_MARKER in str(comment.get("body") or ""):
                try:
                    return int(comment.get("id"))
                except (TypeError, ValueError):
                    continue
        return None

    def publish_acceptance_review(self, pr_number: int, verdict: str, *, note: str) -> Dict[str, Any]:
        """Create-or-update one verified AC-review comment on a PR.

        The reviewer supplies the verdict only after inspecting the diff against
        the packet. ``ACCEPTED`` is rejected unless the PR itself carries the
        explicit, fully evidenced checklist generated by the normal delivery
        flow; the method never calls GitHub's approve endpoint.
        """
        packet = self.review_packet(pr_number)
        body = render_acceptance_review_comment(packet, verdict, note=note)
        existing = self._review_comment_id(pr_number)
        endpoint = ("repos/%s/issues/comments/%s" % (self.repo, existing)
                    if existing else "repos/%s/issues/%s/comments" % (self.repo, pr_number))
        method = "PATCH" if existing else "POST"
        completed = self._gh(["api", endpoint, "--method", method, "--input", "-"],
                             input_text=json.dumps({"body": body}))
        try:
            posted = json.loads(completed.stdout or "{}")
        except (TypeError, ValueError) as exc:
            raise PrPatrolError("GitHub did not return the posted review comment") from exc
        if str(posted.get("body") or "") != body:
            raise PrPatrolError("GitHub review comment could not be verified after publish")
        return {"packet": packet, "comment_id": posted.get("id"), "updated": bool(existing),
                "verdict": str(verdict).upper()}


__all__ = ["ACCEPTANCE_REVIEW_MARKER", "ACCEPTANCE_VERDICTS", "DEFAULT_CADENCE", "PrPatrol",
           "PrPatrolError", "SCHEMA", "assess_acceptance_criteria", "classify_pr",
           "extract_acceptance_criteria", "patrol_due", "render_acceptance_review_comment"]
