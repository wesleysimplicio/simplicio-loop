"""GitHub source-revision capture (#284, item 1 of its Fase 2/Fase 4 checklist).

Issue #284's intake contract requires the Loop to "reread the canonical source
and capture its revision" BEFORE the claim, and to carry `source.revision`,
`source.snapshot_hash`, `source.observed_at` through the task-contract hash and
into the planning receipt/mutation-authority identity tuple, so that a source
edit between planning and execution invalidates the authority instead of being
silently ignored (source drift).

The sibling issue #285 (`simplicio_loop/github_lifecycle.py`) landed the
WRITE side of the GitHub integration — a canonical status comment with
create-or-update + re-query verification. It explicitly does not implement the
READ side (`list_ready`/`get_details`/`reconcile`) that a source snapshot needs.
This module is the missing read-side primitive: it captures a content-addressed
snapshot of one GitHub issue (title, body, labels, milestone, assignees,
comments) via `gh issue view`, using the same fail-closed discipline as
`source_state.py` (a `gh` failure raises rather than fabricating a snapshot;
an injectable `runner`/fixture keeps it testable without a live GitHub call).

GitHub issues have no single "revision" field the way a PR has `headRefOid`.
The revision recorded here is `updated_at` (the issue's own `updatedAt`,
which GitHub bumps on any edit to title/body/labels/state) combined with the
comment count observed at query time; the `snapshot_hash` is the real
tamper-evident identity (any edit to title/body/labels/milestone/assignees/
comment bodies changes it), and is what `planning_gate.py` actually compares
for drift detection. `revision` is kept as a human-legible companion field,
not the source of truth for drift.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

SOURCE_SNAPSHOT_SCHEMA = "simplicio.source-snapshot/v1"

_ISSUE_JSON_FIELDS = "title,body,labels,milestone,assignees,comments,updatedAt,number,url"


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fixture_payload(name: str) -> Optional[Dict[str, Any]]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return json.loads(raw)


def _normalize_issue_view(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Project the `gh issue view --json ...` payload down to the exact fields
    whose hash constitutes "the revision" -- deterministic ordering, only the
    human-relevant content (never ETags/internal ids that churn for free)."""
    labels = sorted(
        str((label or {}).get("name", "")) for label in (raw.get("labels") or []) if isinstance(label, Mapping)
    )
    assignees = sorted(
        str((a or {}).get("login", "")) for a in (raw.get("assignees") or []) if isinstance(a, Mapping)
    )
    milestone = raw.get("milestone") or {}
    comments: List[Dict[str, str]] = []
    for c in raw.get("comments") or []:
        if not isinstance(c, Mapping):
            continue
        comments.append({
            "id": str(c.get("id", "")),
            "author": str((c.get("author") or {}).get("login", "")) if isinstance(c.get("author"), Mapping) else "",
            "body": str(c.get("body", "")),
        })
    return {
        "title": str(raw.get("title", "")),
        "body": str(raw.get("body", "")),
        "labels": labels,
        "milestone": str(milestone.get("title", "")) if isinstance(milestone, Mapping) else "",
        "assignees": assignees,
        "comments": comments,
    }


def capture_github_issue_snapshot(
    repo: str,
    issue: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
    timeout: int = 20,
    observed_at: str = "",
    fixture_env: str = "SIMPLICIO_LOOP_GITHUB_ISSUE_SNAPSHOT_FIXTURE_JSON",
) -> Dict[str, Any]:
    """Capture a `simplicio.source-snapshot/v1` for one GitHub issue.

    Fail-closed: any `gh` invocation failure raises `RuntimeError` -- this
    function never returns a fabricated snapshot for a query it could not
    actually perform. An injected fixture (env var or `runner`) makes this
    testable without hitting the network; `observed_at` defaults to the
    current UTC time when not supplied, for reproducible tests.
    """
    fixture = _fixture_payload(fixture_env)
    if fixture is not None:
        raw = fixture
    else:
        completed = runner(
            ["gh", "issue", "view", str(issue), "--repo", repo, "--json", _ISSUE_JSON_FIELDS],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("gh issue view failed: " + (completed.stderr or "").strip())
        try:
            raw = json.loads(completed.stdout or "{}")
        except ValueError as exc:
            raise RuntimeError(f"gh issue view returned invalid JSON: {exc}") from exc

    normalized = _normalize_issue_view(raw)
    snapshot_hash = content_hash(normalized)
    comment_count = len(normalized["comments"])
    updated_at = str(raw.get("updatedAt", ""))
    revision = f"{updated_at}#comments={comment_count}" if updated_at else f"comments={comment_count}"

    return {
        "schema": SOURCE_SNAPSHOT_SCHEMA,
        "source": {
            "provider": "github",
            "repo": repo,
            "item_id": str(raw.get("number", issue)),
            "url": str(raw.get("url", "")),
            "revision": revision,
            "snapshot_hash": snapshot_hash,
            "observed_at": observed_at or _now_iso(),
        },
        "content": normalized,
    }


def snapshot_hash_of(snapshot: Optional[Mapping[str, Any]]) -> str:
    """Extract `source.snapshot_hash` from a snapshot dict, tolerating None/malformed input."""
    if not snapshot:
        return ""
    return str(((snapshot or {}).get("source") or {}).get("snapshot_hash") or "")


def detect_source_drift(baseline: Optional[Mapping[str, Any]], current: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Compare two snapshots' hashes. Returns a structured verdict, never raises.

    Both snapshots missing/empty is `drifted: False` (nothing to compare, e.g.
    a non-GitHub or fixture-less local run) -- callers that require a snapshot
    must check for its presence themselves before calling this.
    """
    before = snapshot_hash_of(baseline)
    after = snapshot_hash_of(current)
    if not before and not after:
        return {"drifted": False, "reason_code": "no_snapshot", "before": before, "after": after}
    drifted = before != after
    return {
        "drifted": drifted,
        "reason_code": "source_changed" if drifted else "source_unchanged",
        "before": before,
        "after": after,
    }


__all__ = [
    "SOURCE_SNAPSHOT_SCHEMA",
    "content_hash",
    "capture_github_issue_snapshot",
    "snapshot_hash_of",
    "detect_source_drift",
]
