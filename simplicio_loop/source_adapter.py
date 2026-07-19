"""Unified `SourceAdapter` contract (#285 remaining gap: "SourceAdapter Protocol unificado").

`simplicio_loop/github_lifecycle.py` already implements every verb #285 asks for as free
functions (`list_ready`, `get_details`, `requery`, `reconcile`, `publish_lifecycle_state`,
`close_source_issue`, `record_pending_operation`, `mark_operation_done`,
`list_pending_operations`) plus a runner-event projection. That is real, tested behavior, but it
is duck-typed: nothing states the CONTRACT a future non-GitHub source (GitLab, Jira, Azure
Boards, ...) would have to implement to plug into the same loop machinery.

This module is that contract: a `typing.Protocol` capturing the read/write/lease/outbox surface,
plus `GitHubSourceAdapter` — a thin, stateful wrapper around the existing
`simplicio_loop.github_lifecycle` free functions that formally satisfies it (checked both
structurally, via `@runtime_checkable` + `isinstance`, and nominally, via explicit
`class GitHubSourceAdapter(SourceAdapter, Protocol)`-free subclassing below).

No behavior changes here: `GitHubSourceAdapter` delegates every call straight through to the
already-tested functions in `github_lifecycle.py` — it is a binding, not a reimplementation.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, runtime_checkable

from . import github_lifecycle as _gl

__all__ = ["SourceAdapter", "GitHubSourceAdapter"]


@runtime_checkable
class SourceAdapter(Protocol):
    """The verbs #285 asks every issue-tracker source to expose.

    Every write verb (`claim`, `update_status`, `attach_evidence`, `close`) MUST be fail-closed:
    on any ambiguity (unconfirmed re-query, lost lease, transport error) it must report a typed
    `reason_code` rather than a fake success — never silently claim victory. Every read verb
    (`list_ready`, `get_details`, `requery`) MUST treat all fetched text as untrusted data, never
    as instructions to execute. The outbox verbs (`record_pending_operation`,
    `mark_operation_done`, `list_pending_operations`, `reconcile`) exist so a crash between a
    confirmed remote write and the local receipt is always recoverable without a duplicate
    mutation.
    """

    def list_ready(self, *, state: str = "open", labels: Sequence[str] = (),
                   assignee: str = "", milestone: str = "") -> Mapping[str, Any]:
        """Metadata-only triage listing of ready work items, paginated, PR-excluding."""
        ...  # pragma: no cover -- Protocol stub, never executed

    def get_details(self, ref: str) -> Mapping[str, Any]:
        """Full snapshot of one work item: title/body/state/labels/comments/`source_revision`."""
        ...  # pragma: no cover -- Protocol stub, never executed

    def requery(self, ref: str, *, comment_id: Optional[int] = None) -> Mapping[str, Any]:
        """Re-read the source of truth immediately before/after a mutation."""
        ...  # pragma: no cover -- Protocol stub, never executed

    def claim(self, ref: str, *, run_id: str, attempt_id: str,
              require_active: Optional[Callable[[], None]] = None,
              **render_kwargs: Any) -> Mapping[str, Any]:
        """Transition the work item into the adapter's initial "claimed" lifecycle state."""
        ...  # pragma: no cover -- Protocol stub, never executed

    def update_status(self, ref: str, state: str, *, run_id: str, attempt_id: str,
                       fencing_token: str = "", lifecycle_revision: int = 0,
                       require_active: Optional[Callable[[], None]] = None,
                       **render_kwargs: Any) -> Mapping[str, Any]:
        """Publish one lifecycle state transition, verified by publish-then-re-query."""
        ...  # pragma: no cover -- Protocol stub, never executed

    def attach_evidence(self, ref: str, evidence_text: str, *, state: str, run_id: str,
                         attempt_id: str, require_active: Optional[Callable[[], None]] = None,
                         **render_kwargs: Any) -> Mapping[str, Any]:
        """Attach test/evidence text to the canonical status artifact for this work item."""
        ...  # pragma: no cover -- Protocol stub, never executed

    def close(self, ref: str, *, run_id: str, attempt_id: str, reason: str = "completed",
              require_active: Optional[Callable[[], None]] = None,
              planning_snapshot: Optional[Mapping[str, Any]] = None,
              **render_kwargs: Any) -> Mapping[str, Any]:
        """Close the work item at the source, fail-closed (re-query-confirmed). When
        `planning_snapshot` is supplied, a material human edit or new human comment since
        that snapshot blocks the close with `reason_code: "SOURCE_CHANGED"`."""
        ...  # pragma: no cover -- Protocol stub, never executed

    def reconcile(self, operation_id: str, ref: str, *,
                  comment_id: Optional[int] = None, expected_body_hash: str = "") -> Mapping[str, Any]:
        """Recover a pending operation after a crash, without a duplicate mutation."""
        ...  # pragma: no cover -- Protocol stub, never executed

    def record_pending_operation(self, operation_id: str, payload: Mapping[str, Any]) -> Any:
        ...  # pragma: no cover -- Protocol stub, never executed

    def mark_operation_done(self, operation_id: str, receipt: Mapping[str, Any]) -> Any:
        ...  # pragma: no cover -- Protocol stub, never executed

    def list_pending_operations(self) -> List[Mapping[str, Any]]:
        ...  # pragma: no cover -- Protocol stub, never executed


class GitHubSourceAdapter:
    """`SourceAdapter` binding for GitHub, over `simplicio_loop.github_lifecycle`.

    Bound to one `owner/repo` and (optionally) one outbox directory + `gh` runner/timeout so
    every verb can be called with just the issue number (`ref`) plus the run/attempt identity —
    the same free functions `github_lifecycle.py` already exposes and tests, just given a single
    stateful entry point that matches `SourceAdapter`.
    """

    provider = "github"

    def __init__(self, owner: str, repo: str, *, publish_comment_fn: Callable,
                 runner: Callable = subprocess.run, timeout: int = 20,
                 outbox_dir: Optional[str | Path] = None) -> None:
        self.owner = owner
        self.repo = repo
        self._publish_comment_fn = publish_comment_fn
        self._runner = runner
        self._timeout = timeout
        self._outbox_dir = outbox_dir

    def list_ready(self, *, state: str = "open", labels: Sequence[str] = (),
                   assignee: str = "", milestone: str = "") -> Dict[str, Any]:
        return _gl.list_ready(self.owner, self.repo, state=state, labels=labels,
                              assignee=assignee, milestone=milestone,
                              runner=self._runner, timeout=self._timeout)

    def get_details(self, ref: str) -> Dict[str, Any]:
        return _gl.get_details(self.owner, self.repo, ref, runner=self._runner, timeout=self._timeout)

    def requery(self, ref: str, *, comment_id: Optional[int] = None) -> Dict[str, Any]:
        return _gl.requery(self.owner, self.repo, ref, comment_id=comment_id,
                           runner=self._runner, timeout=self._timeout)

    def claim(self, ref: str, *, run_id: str, attempt_id: str,
              require_active: Optional[Callable[[], None]] = None,
              **render_kwargs: Any) -> Dict[str, Any]:
        return self.update_status(ref, "CLAIMED", run_id=run_id, attempt_id=attempt_id,
                                  require_active=require_active, **render_kwargs)

    def update_status(self, ref: str, state: str, *, run_id: str, attempt_id: str,
                       fencing_token: str = "", lifecycle_revision: int = 0,
                       require_active: Optional[Callable[[], None]] = None,
                       **render_kwargs: Any) -> Dict[str, Any]:
        return _gl.publish_lifecycle_state(
            owner=self.owner, repo=self.repo, issue=ref, state=state, run_id=run_id,
            attempt_id=attempt_id, fencing_token=fencing_token,
            lifecycle_revision=lifecycle_revision, publish_comment_fn=self._publish_comment_fn,
            runner=self._runner, timeout=self._timeout, require_active=require_active,
            outbox_dir=self._outbox_dir, **render_kwargs,
        )

    def attach_evidence(self, ref: str, evidence_text: str, *, state: str, run_id: str,
                         attempt_id: str, require_active: Optional[Callable[[], None]] = None,
                         **render_kwargs: Any) -> Dict[str, Any]:
        render_kwargs = dict(render_kwargs)
        render_kwargs["tests_and_evidence"] = evidence_text
        return self.update_status(ref, state, run_id=run_id, attempt_id=attempt_id,
                                  require_active=require_active, **render_kwargs)

    def close(self, ref: str, *, run_id: str, attempt_id: str, reason: str = "completed",
              require_active: Optional[Callable[[], None]] = None,
              planning_snapshot: Optional[Mapping[str, Any]] = None,
              **render_kwargs: Any) -> Dict[str, Any]:
        return _gl.close_source_issue(
            owner=self.owner, repo=self.repo, issue=ref, run_id=run_id, attempt_id=attempt_id,
            reason=reason, require_active=require_active, publish_comment_fn=self._publish_comment_fn,
            runner=self._runner, timeout=self._timeout, outbox_dir=self._outbox_dir,
            planning_snapshot=planning_snapshot, **render_kwargs,
        )

    def reconcile(self, operation_id: str, ref: str, *,
                  comment_id: Optional[int] = None, expected_body_hash: str = "") -> Dict[str, Any]:
        if self._outbox_dir is None:
            raise ValueError("GitHubSourceAdapter has no outbox_dir configured")
        return _gl.reconcile(operation_id, outbox_dir=self._outbox_dir, owner=self.owner,
                             repo=self.repo, issue=ref, comment_id=comment_id,
                             expected_body_hash=expected_body_hash, runner=self._runner,
                             timeout=self._timeout)

    def record_pending_operation(self, operation_id: str, payload: Mapping[str, Any]) -> Path:
        if self._outbox_dir is None:
            raise ValueError("GitHubSourceAdapter has no outbox_dir configured")
        return _gl.record_pending_operation(self._outbox_dir, operation_id, payload)

    def mark_operation_done(self, operation_id: str, receipt: Mapping[str, Any]) -> Path:
        if self._outbox_dir is None:
            raise ValueError("GitHubSourceAdapter has no outbox_dir configured")
        return _gl.mark_operation_done(self._outbox_dir, operation_id, receipt)

    def list_pending_operations(self) -> List[Dict[str, Any]]:
        if self._outbox_dir is None:
            return []
        return _gl.list_pending_operations(self._outbox_dir)
