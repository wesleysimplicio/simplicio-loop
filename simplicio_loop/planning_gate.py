"""Planning receipt + mutation authority (#284).

The runner already refuses `execute_operator()` without a *fresh* mapper/plan/
operator-preflight trio, a passing `validate_plan()`, an unchanged repo state
since planning, and an operator target confined to the plan's
`candidate_targets` (see `simplicio_loop/runner.py::execute_operator`). What was
missing, per issue #284, is a single, explicit, hash-bound **planning receipt**
that ties those checks together into one artifact plus a **mutation authority**
token derived from it — so a future caller cannot invoke the operator boundary
while silently skipping part of the intake, and any drift in the underlying
contract/plan/lease invalidates the authority immediately (fail-closed, never a
stale "yes").

This module is intentionally data-only and model-free, same discipline as
`plan_contract.py` and `quality_matrix.py`: it reads/writes a JSON receipt and a
derived token, and never invents evidence for a gate that wasn't actually
satisfied.

Scope landed here (opt-in, additive — a run that never builds a planning
receipt keeps working exactly as before):

  * `build_planning_receipt(...)` combines the task contract hash, the plan
    hash, the AC <-> plan coverage already computed by `validate_plan`, and the
    optional lease/fencing identity into one receipt with a
    `ready_for_mutation` verdict.
  * `mutation_authority_token(...)` derives a stable token from that identity
    tuple; `verify_mutation_authority(...)` recomputes it from the CURRENT
    identity tuple and fails closed on any mismatch (stale plan hash, task
    contract changed, lease/fencing rotated, etc.).

GitHub source-revision capture is now wired in for real: `source_snapshot.py`
(added for this increment) captures a content-addressed snapshot of the
canonical GitHub issue (title/body/labels/milestone/assignees/comments) via
`gh issue view`, fail-closed on any `gh` failure. Its `snapshot_hash` is an
OPTIONAL sixth member of the mutation-authority identity tuple here: when a
caller supplies `source_snapshot_hash`, any edit to the source between
planning and execution changes the hash and invalidates the authority exactly
like a stale plan hash or rotated lease does. Omitting it (the default, `""`)
preserves the exact previous behavior for local/non-GitHub runs and every
existing fixture.

Not yet implemented (tracked as follow-up, not claimed here): the full
`simplicio.task-intake/v1` envelope (scope in/out, dependencies, risks,
rollback, impact map), plan v2's DAG/parallelizable-step metadata, and making
`execute_operator()` require this receipt unconditionally (today it is opt-in
via `SIMPLICIO_REQUIRE_MUTATION_AUTHORITY`, since flipping the default to
mandatory for `execute_operator_batch()` too would need every batch-dispatch
test fixture in the suite updated first -- a materially larger, separate
change from wiring the gate itself).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

PLANNING_RECEIPT_SCHEMA = "simplicio.planning-receipt/v1"
RECEIPT_FILENAME = "planning-receipt.json"


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(obj: Any) -> str:
    """Deterministic sha256 over a JSON-serializable structure."""
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def mutation_authority_token(*, run_id: str, attempt: int, task_contract_hash: str,
                            plan_hash: str, lease_id: str = "", fencing_token: str = "",
                            source_snapshot_hash: str = "") -> str:
    """Derive the mutation-authority token from the exact identity tuple it authorizes.

    Any change to run/attempt/contract/plan/lease/fence/source-snapshot changes the
    token, so an authority minted for one identity can never silently validate a
    different one. `source_snapshot_hash` defaults to `""` -- a run that never
    captures a GitHub source snapshot (local/non-GitHub sources) is unaffected.
    """
    payload = {
        "run_id": str(run_id or ""),
        "attempt": int(attempt or 0),
        "task_contract_hash": str(task_contract_hash or ""),
        "plan_hash": str(plan_hash or ""),
        "lease_id": str(lease_id or ""),
        "fencing_token": str(fencing_token or ""),
        "source_snapshot_hash": str(source_snapshot_hash or ""),
    }
    return content_hash(payload)


def verify_mutation_authority(authority: str, *, run_id: str, attempt: int,
                              task_contract_hash: str, plan_hash: str,
                              lease_id: str = "", fencing_token: str = "",
                              source_snapshot_hash: str = "") -> bool:
    """Fail-closed re-check: recompute the token from the CURRENT identity and compare."""
    if not str(authority or "").strip():
        return False
    expected = mutation_authority_token(
        run_id=run_id, attempt=attempt, task_contract_hash=task_contract_hash,
        plan_hash=plan_hash, lease_id=lease_id, fencing_token=fencing_token,
        source_snapshot_hash=source_snapshot_hash,
    )
    return expected == authority


def build_planning_receipt(
    *,
    run_id: str,
    attempt: int,
    contract: Mapping[str, Any],
    plan: Mapping[str, Any],
    plan_validation: Mapping[str, Any],
    lease_id: str = "",
    fencing_token: str = "",
    source_snapshot: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the #284 planning receipt from already-computed intake artifacts.

    `plan_validation` is the dict returned by `plan_contract.validate_plan()` —
    this function does not re-derive plan/AC coverage, it packages the verdict
    already produced by the existing, tested validator into one hash-bound,
    immutable-once-ready receipt.

    `source_snapshot` is the optional `simplicio.source-snapshot/v1` dict from
    `source_snapshot.capture_github_issue_snapshot()`. When supplied, its
    `source.snapshot_hash` is folded into the mutation-authority identity tuple
    (source drift then invalidates the authority the same way a stale plan
    hash does) and the `source` block is embedded in the receipt for
    traceability. Omitting it keeps the receipt identical to before this
    increment.
    """
    task_contract_hash = str(contract.get("collection_hash") or content_hash(contract))
    plan_hash = content_hash(plan)
    plan_valid = bool(plan_validation.get("valid"))
    ready = plan_valid
    source = dict((source_snapshot or {}).get("source") or {})
    source_snapshot_hash = str(source.get("snapshot_hash") or "")
    authority = ""
    if ready:
        authority = mutation_authority_token(
            run_id=run_id, attempt=attempt, task_contract_hash=task_contract_hash,
            plan_hash=plan_hash, lease_id=lease_id, fencing_token=fencing_token,
            source_snapshot_hash=source_snapshot_hash,
        )
    receipt: Dict[str, Any] = {
        "schema": PLANNING_RECEIPT_SCHEMA,
        "run_id": str(run_id or ""),
        "attempt": int(attempt or 0),
        "lease_id": str(lease_id or ""),
        "fencing_token": str(fencing_token or ""),
        "task_contract_hash": task_contract_hash,
        "plan_hash": plan_hash,
        "plan_validation": {
            "valid": plan_valid,
            "errors": list(plan_validation.get("errors") or []),
            "warnings": list(plan_validation.get("warnings") or []),
            "checked_tasks": plan_validation.get("checked_tasks", 0),
        },
        "ready_for_mutation": ready,
        "mutation_authority": authority,
    }
    if source:
        receipt["source"] = source
    return receipt


def receipt_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / RECEIPT_FILENAME


def load_planning_receipt(run_dir: str | Path) -> Dict[str, Any] | None:
    path = receipt_path(run_dir)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def evaluate_mutation_authority(
    run_dir: str | Path, *, run_id: str, attempt: int, task_contract_hash: str,
    plan_hash: str, lease_id: str = "", fencing_token: str = "",
    source_snapshot_hash: str = "",
) -> Dict[str, Any]:
    """Fail-closed gate: load the on-disk receipt and re-verify its authority
    against the CURRENT identity tuple the caller is about to execute with.

    `source_snapshot_hash`, when supplied, must match the hash the receipt was
    minted with -- a GitHub issue edited between planning and execution (source
    drift) invalidates the authority exactly like a stale plan hash does.

    Returns a structured verdict (never raises) so callers can render a clear
    BLOCKED reason instead of a bare exception.
    """
    receipt = load_planning_receipt(run_dir)
    if not receipt:
        return {"ok": False, "reason_code": "planning_receipt_missing",
                "reason": f"{RECEIPT_FILENAME} is missing or unreadable"}
    if receipt.get("schema") != PLANNING_RECEIPT_SCHEMA:
        return {"ok": False, "reason_code": "planning_receipt_schema_invalid",
                "reason": "planning receipt schema mismatch"}
    if not receipt.get("ready_for_mutation"):
        return {"ok": False, "reason_code": "planning_not_ready",
                "reason": "planning receipt is not ready_for_mutation"}
    authority = str(receipt.get("mutation_authority") or "")
    receipt_source_hash = str((receipt.get("source") or {}).get("snapshot_hash") or "")
    if not verify_mutation_authority(
        authority, run_id=run_id, attempt=attempt, task_contract_hash=task_contract_hash,
        plan_hash=plan_hash, lease_id=lease_id, fencing_token=fencing_token,
        source_snapshot_hash=receipt_source_hash,
    ):
        return {"ok": False, "reason_code": "mutation_authority_invalid",
                "reason": "mutation authority does not match the current run/attempt/contract/plan/lease/fence identity"}
    if source_snapshot_hash and receipt_source_hash and source_snapshot_hash != receipt_source_hash:
        return {"ok": False, "reason_code": "source_drift",
                "reason": "GitHub source snapshot changed since planning; authority invalidated"}
    return {"ok": True, "reason_code": "mutation_authority_verified",
            "reason": "mutation authority verified for the current identity tuple"}


__all__ = [
    "PLANNING_RECEIPT_SCHEMA",
    "RECEIPT_FILENAME",
    "content_hash",
    "mutation_authority_token",
    "verify_mutation_authority",
    "build_planning_receipt",
    "receipt_path",
    "load_planning_receipt",
    "evaluate_mutation_authority",
]
