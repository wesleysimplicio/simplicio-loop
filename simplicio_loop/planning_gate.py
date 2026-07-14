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

Not yet implemented (tracked as follow-up, not claimed here): GitHub source
revision capture (depends on the sibling issue #285's adapter), the full
`simplicio.task-intake/v1` envelope (scope in/out, dependencies, risks,
rollback, impact map), plan v2's DAG/parallelizable-step metadata, and making
`execute_operator()` require this receipt unconditionally (today it is opt-in
via `require_mutation_authority=True`, since flipping the default to mandatory
would need every existing run fixture in the test suite updated first).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

PLANNING_RECEIPT_SCHEMA = "simplicio.planning-receipt/v1"
RECEIPT_FILENAME = "planning-receipt.json"


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(obj: Any) -> str:
    """Deterministic sha256 over a JSON-serializable structure."""
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def mutation_authority_token(*, run_id: str, attempt: int, task_contract_hash: str,
                            plan_hash: str, lease_id: str = "", fencing_token: str = "") -> str:
    """Derive the mutation-authority token from the exact identity tuple it authorizes.

    Any change to run/attempt/contract/plan/lease/fence changes the token, so an
    authority minted for one identity can never silently validate a different one.
    """
    payload = {
        "run_id": str(run_id or ""),
        "attempt": int(attempt or 0),
        "task_contract_hash": str(task_contract_hash or ""),
        "plan_hash": str(plan_hash or ""),
        "lease_id": str(lease_id or ""),
        "fencing_token": str(fencing_token or ""),
    }
    return content_hash(payload)


def verify_mutation_authority(authority: str, *, run_id: str, attempt: int,
                              task_contract_hash: str, plan_hash: str,
                              lease_id: str = "", fencing_token: str = "") -> bool:
    """Fail-closed re-check: recompute the token from the CURRENT identity and compare."""
    if not str(authority or "").strip():
        return False
    expected = mutation_authority_token(
        run_id=run_id, attempt=attempt, task_contract_hash=task_contract_hash,
        plan_hash=plan_hash, lease_id=lease_id, fencing_token=fencing_token,
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
) -> Dict[str, Any]:
    """Build the #284 planning receipt from already-computed intake artifacts.

    `plan_validation` is the dict returned by `plan_contract.validate_plan()` —
    this function does not re-derive plan/AC coverage, it packages the verdict
    already produced by the existing, tested validator into one hash-bound,
    immutable-once-ready receipt.
    """
    task_contract_hash = str(contract.get("collection_hash") or content_hash(contract))
    plan_hash = content_hash(plan)
    plan_valid = bool(plan_validation.get("valid"))
    ready = plan_valid
    authority = ""
    if ready:
        authority = mutation_authority_token(
            run_id=run_id, attempt=attempt, task_contract_hash=task_contract_hash,
            plan_hash=plan_hash, lease_id=lease_id, fencing_token=fencing_token,
        )
    return {
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
) -> Dict[str, Any]:
    """Fail-closed gate: load the on-disk receipt and re-verify its authority
    against the CURRENT identity tuple the caller is about to execute with.

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
    if not verify_mutation_authority(
        authority, run_id=run_id, attempt=attempt, task_contract_hash=task_contract_hash,
        plan_hash=plan_hash, lease_id=lease_id, fencing_token=fencing_token,
    ):
        return {"ok": False, "reason_code": "mutation_authority_invalid",
                "reason": "mutation authority does not match the current run/attempt/contract/plan/lease/fence identity"}
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
