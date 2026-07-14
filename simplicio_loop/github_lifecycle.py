"""GitHub issue lifecycle adapter (#285) — one canonical, idempotent status comment.

Issue #285 asks for the full GitHub source adapter: `list_ready`/`get_details`/
`claim`/`update_status`/`attach_evidence`/`close`/`requery`/`reconcile`, an
outbox, lease/fencing integration, and duplicate-comment recovery. That is a
multi-week surface. This module lands the real, testable core the rest of that
surface depends on and reuses rather than duplicates: `#295` already built a
fail-closed, idempotent, marker-based create-or-update primitive
(`scripts/pr_evidence.py::publish_comment`/`find_existing_comment` — no shell
interpolation, JSON payload on stdin, `PublishError` on any `gh` failure). This
module adds, on top of that primitive:

  * a minimal, VALIDATED lifecycle state machine (`LIFECYCLE_STATES`,
    `validate_transition`) — an event that isn't a legal transition from the
    current state is rejected with a reason code, never silently accepted;
  * a deterministic, sanitized renderer for the canonical status comment
    (`render_lifecycle_comment`), using ITS OWN marker
    (`<!-- simplicio-loop:lifecycle-status:v1 -->`) distinct from the #295
    evidence-comment marker, so the two comments never collide on the same
    issue;
  * `publish_lifecycle_state`: publish (create-or-update, idempotent, via the
    #295 primitive) THEN immediately re-query the same comment and compare the
    observed body hash against the expected one — the issue's "só retornar
    sucesso se o ID e body hash observados coincidirem" requirement — producing
    a `simplicio.github-lifecycle-receipt/v1` receipt rather than a bare bool.

Deliberately out of scope for this increment (tracked, not claimed done):
lease/fencing-gated ownership of the comment, an outbox for crash recovery
between the remote write and the local receipt, duplicate-comment reconciliation
across two authors, and the `list_ready`/`get_details`/`reconcile` read-side
verbs. See the module docstring cross-references in the PR body for the exact
remaining checklist.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

LIFECYCLE_SCHEMA = "simplicio.github-lifecycle-receipt/v1"
LIFECYCLE_COMMENT_MARKER = "<!-- simplicio-loop:lifecycle-status:v1 -->"

# #285 "Estados mínimos"
LIFECYCLE_STATES: Tuple[str, ...] = (
    "DISCOVERED",
    "CLAIMED",
    "PLANNED",
    "IN_PROGRESS",
    "VERIFYING",
    "BLOCKED",
    "PAUSED_NETWORK",
    "AWAITING_DECISION",
    "PR_OPEN",
    "MERGE_READY",
    "MERGED",
    "CLOSING",
    "CLOSE_PENDING_RECONCILIATION",
    "CLOSED",
    "RELEASED",
)

# Forward-flow transitions (the "happy path" the issue's ASCII diagram describes,
# plus the terminal/side states every stage can fall into). Regressions (e.g.
# PR_OPEN -> IN_PROGRESS) are only ever valid with an explicit reason_code, never
# as a bare state hop -- see `validate_transition`.
_FORWARD_TRANSITIONS: Dict[str, Sequence[str]] = {
    "DISCOVERED": ("CLAIMED",),
    "CLAIMED": ("PLANNED", "BLOCKED", "PAUSED_NETWORK"),
    "PLANNED": ("IN_PROGRESS", "AWAITING_DECISION", "BLOCKED", "PAUSED_NETWORK"),
    "IN_PROGRESS": ("VERIFYING", "BLOCKED", "PAUSED_NETWORK", "AWAITING_DECISION"),
    "VERIFYING": ("PR_OPEN", "BLOCKED", "AWAITING_DECISION"),
    "PR_OPEN": ("MERGE_READY", "BLOCKED", "AWAITING_DECISION"),
    "MERGE_READY": ("MERGED", "BLOCKED"),
    "MERGED": ("CLOSING",),
    "CLOSING": ("CLOSED", "CLOSE_PENDING_RECONCILIATION"),
    "CLOSE_PENDING_RECONCILIATION": ("CLOSED",),
    "CLOSED": ("RELEASED",),
    # side states can resume forward once the blocker clears
    "BLOCKED": ("IN_PROGRESS", "PLANNED", "AWAITING_DECISION"),
    "PAUSED_NETWORK": ("CLAIMED", "PLANNED", "IN_PROGRESS"),
    "AWAITING_DECISION": ("PLANNED", "IN_PROGRESS", "VERIFYING", "BLOCKED"),
    "RELEASED": (),
}

# Reason codes that authorize an explicit REGRESSION (a hop not in
# `_FORWARD_TRANSITIONS`) -- per the issue: "Regressões de estado só são
# permitidas com reason code explícito".
REGRESSION_REASON_CODES = frozenset({
    "SOURCE_CHANGED", "CHECKS_REGRESSED", "REVIEW_REOPENED", "LEASE_REASSIGNED",
    "DELIVERY_REGRESSED",
})


def validate_transition(from_state: str, to_state: str, *, reason_code: str = "") -> Dict[str, Any]:
    """Validate one lifecycle transition. Never raises; returns a structured verdict.

    A duplicate event (to_state == from_state) is a no-op, always valid --
    "Eventos duplicados são no-op idempotente" per the issue text.
    """
    if from_state not in LIFECYCLE_STATES:
        return {"ok": False, "reason_code": "unknown_from_state", "reason": f"unknown state {from_state!r}"}
    if to_state not in LIFECYCLE_STATES:
        return {"ok": False, "reason_code": "unknown_to_state", "reason": f"unknown state {to_state!r}"}
    if to_state == from_state:
        return {"ok": True, "reason_code": "duplicate_noop", "reason": "duplicate event, no-op"}
    if to_state in _FORWARD_TRANSITIONS.get(from_state, ()):
        return {"ok": True, "reason_code": "transition_valid", "reason": f"{from_state} -> {to_state} is a valid forward transition"}
    if reason_code in REGRESSION_REASON_CODES:
        return {"ok": True, "reason_code": reason_code, "reason": f"{from_state} -> {to_state} authorized as a regression ({reason_code})"}
    return {"ok": False, "reason_code": "transition_invalid",
            "reason": f"{from_state} -> {to_state} is not a valid forward transition and no regression reason_code was given"}


def _redact(text: str) -> str:
    """Best-effort redaction of anything that looks like a secret/token before it
    ever reaches a public GitHub comment -- #285's "sem segredos" principle."""
    import re

    text = re.sub(r"(gh[pousr]_[A-Za-z0-9]{20,})", "[REDACTED-TOKEN]", text)
    text = re.sub(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+", r"\1: [REDACTED]", text)
    return text


def render_lifecycle_comment(
    *,
    state: str,
    run_id: str,
    attempt_id: str,
    agent_id: str = "",
    runtime: str = "",
    device: str = "",
    lease_id: str = "",
    fencing_token: str = "",
    branch: str = "",
    worktree: str = "",
    updated_at: str = "",
    goal: str = "",
    scope: str = "",
    acceptance_criteria: Optional[Sequence[Mapping[str, Any]]] = None,
    plan_steps: Optional[Sequence[str]] = None,
    progress: str = "",
    blockers: Optional[Sequence[str]] = None,
    tests_and_evidence: str = "",
    delivery: str = "",
) -> str:
    """Deterministic renderer for the ONE canonical status comment (#285).

    Every field is sanitized (`_redact`) before rendering -- a secret or a raw
    stack trace in an upstream field must never reach the public comment.
    """
    if state not in LIFECYCLE_STATES:
        raise ValueError(f"unknown lifecycle state: {state!r}")
    acs = list(acceptance_criteria or [])
    steps = list(plan_steps or [])
    blocked = list(blockers or [])

    lines: List[str] = []
    lines.append("## \U0001F501 Simplicio Loop — status da execução")
    lines.append("")
    lines.append("| Campo | Valor |")
    lines.append("|---|---|")
    lines.append(f"| Estado | {state} |")
    lines.append(f"| Run / attempt | {run_id} / {attempt_id} |")
    if agent_id:
        lines.append(f"| Agente | {agent_id} |")
    if runtime or device:
        lines.append(f"| Runtime / device | {runtime} / {device} |")
    if lease_id or fencing_token:
        lines.append(f"| Lease / fence | {lease_id} / {fencing_token} |")
    if branch or worktree:
        lines.append(f"| Branch / worktree | {branch} / {worktree} |")
    if updated_at:
        lines.append(f"| Atualizado | {updated_at} |")
    lines.append("")

    if goal or scope:
        lines.append("### Objetivo e escopo")
        if goal:
            lines.append(goal)
        if scope:
            lines.append(scope)
        lines.append("")

    if acs:
        lines.append("### Critérios de aceite")
        for ac in acs:
            ac_id = str(ac.get("id") or "").strip()
            text = str(ac.get("text") or "").strip()
            done = bool(ac.get("done"))
            box = "x" if done else " "
            label = f"**{ac_id}** " if ac_id else ""
            lines.append(f"- [{box}] {label}{text}".rstrip())
        lines.append("")

    if steps:
        lines.append("### Plano passo a passo")
        for idx, step in enumerate(steps, start=1):
            lines.append(f"{idx}. {step}")
        lines.append("")

    if progress or blocked:
        lines.append("### Progresso e blockers")
        if progress:
            lines.append(progress)
        for b in blocked:
            lines.append(f"- BLOCKER: {b}")
        lines.append("")

    if tests_and_evidence:
        lines.append("### Testes e evidências")
        lines.append(tests_and_evidence)
        lines.append("")

    if delivery:
        lines.append("### Entrega")
        lines.append(delivery)
        lines.append("")

    lines.append(LIFECYCLE_COMMENT_MARKER)
    body = "\n".join(lines).rstrip() + "\n"
    return _redact(body)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def operation_id(*, provider: str, repo: str, issue: str, run_id: str, attempt_id: str,
                 fencing_token: str, lifecycle_revision: int, operation_kind: str) -> str:
    """Deterministic operation id (#285's idempotency-key tuple)."""
    payload = {
        "provider": provider, "repo": repo, "issue": str(issue), "run_id": run_id,
        "attempt_id": attempt_id, "fencing_token": fencing_token,
        "lifecycle_revision": int(lifecycle_revision), "operation_kind": operation_kind,
    }
    return content_hash(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def _get_comment_body(owner: str, repo: str, comment_id: int,
                      runner: Callable, timeout: int) -> Optional[str]:
    completed = runner(
        ["gh", "api", "repos/%s/%s/issues/comments/%s" % (owner, repo, comment_id)],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if completed.returncode != 0:
        return None
    try:
        return json.loads(completed.stdout).get("body")
    except ValueError:
        return None


def publish_lifecycle_state(
    *,
    owner: str,
    repo: str,
    issue: str,
    state: str,
    run_id: str,
    attempt_id: str,
    fencing_token: str = "",
    lifecycle_revision: int = 0,
    publish_comment_fn: Callable,
    runner: Callable = subprocess.run,
    timeout: int = 20,
    **render_kwargs: Any,
) -> Dict[str, Any]:
    """Publish one lifecycle state to the ONE canonical comment, then re-query and
    confirm the observed id/body-hash match what was just written.

    `publish_comment_fn` is `scripts.pr_evidence.publish_comment` injected by the
    caller (kept as a parameter, not a hard import, so this module has no import
    cycle with `scripts/`) -- it already provides idempotent create-or-update via
    `LIFECYCLE_COMMENT_MARKER`. Returns a `simplicio.github-lifecycle-receipt/v1`
    receipt; never raises for an ordinary publish/re-query mismatch (that is
    reported as `verified: False`, `outcome: "blocked"` instead), but a
    `PublishError` from the underlying transport (auth/network/permission)
    propagates -- fail-closed, never silently swallowed into a fake "verified".
    """
    body = render_lifecycle_comment(state=state, run_id=run_id, attempt_id=attempt_id,
                                    fencing_token=fencing_token, **render_kwargs)
    expected_hash = content_hash(body)
    op_id = operation_id(provider="github", repo=f"{owner}/{repo}", issue=str(issue),
                        run_id=run_id, attempt_id=attempt_id, fencing_token=fencing_token,
                        lifecycle_revision=lifecycle_revision, operation_kind=f"update_status:{state}")

    result = publish_comment_fn(owner, repo, str(issue), body, marker=LIFECYCLE_COMMENT_MARKER,
                                runner=runner, timeout=timeout)
    comment_id = result.get("id")
    observed_body = _get_comment_body(owner, repo, comment_id, runner, timeout) if comment_id is not None else None
    observed_hash = content_hash(observed_body) if observed_body is not None else ""
    verified = comment_id is not None and observed_hash == expected_hash

    return {
        "schema": LIFECYCLE_SCHEMA,
        "operation_id": op_id,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "fencing_token": fencing_token,
        "repo": f"{owner}/{repo}",
        "issue": str(issue),
        "state": state,
        "comment_id": comment_id,
        "expected_body_hash": expected_hash,
        "observed_body_hash": observed_hash,
        "action": result.get("action"),
        "outcome": (result.get("action") or "blocked") if verified else "blocked",
        "verified": verified,
    }


__all__ = [
    "LIFECYCLE_SCHEMA",
    "LIFECYCLE_COMMENT_MARKER",
    "LIFECYCLE_STATES",
    "REGRESSION_REASON_CODES",
    "validate_transition",
    "render_lifecycle_comment",
    "content_hash",
    "operation_id",
    "publish_lifecycle_state",
]
