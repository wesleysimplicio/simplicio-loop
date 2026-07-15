"""Planning receipt + mutation authority (#284).

The runner already refuses `execute_operator()` without a *fresh* mapper/plan/
operator-preflight trio, a passing `validate_plan()`, an unchanged repo state
since planning, and an operator target confined to the plan's
`candidate_targets` (see `simplicio_loop/runner.py::execute_operator`). Issue
#284 adds a single, explicit, hash-bound **planning receipt** that ties those
checks into one artifact plus a **mutation authority** token derived from it
— so a future caller cannot invoke the operator boundary while silently skipping
part of the intake, and any drift in the underlying contract/plan/lease/source
invalidates the authority immediately (fail-closed, never a stale "yes").

This module is intentionally data-only and model-free, same discipline as
`plan_contract.py` and `quality_matrix.py`: it reads/writes a JSON receipt and a
derived token, and never invents evidence for a gate that wasn't actually
satisfied.

Public surface:

  * ``build_planning_receipt(...)`` — packages the ``validate_plan()`` verdict,
    source snapshot, and lease identity into one hash-bound receipt.
  * ``mutation_authority_token(...)`` — derives a stable token from the identity
    tuple (run/attempt/contract/plan/lease/fence/source_revision).
  * ``verify_mutation_authority(...)`` — recomputes the token from the CURRENT
    identity and fails closed on any mismatch.
  * ``evaluate_mutation_authority(...)`` — full fail-closed gate that loads the
    on-disk receipt, checks verdict, expiry, and identity; returns structured
    reason codes including ``STALE_SOURCE``, ``LEASE_LOST``,
    ``AWAITING_DECISION``, and ``AUTHORITY_EXPIRED``.

Verdict codes emitted by ``evaluate_mutation_authority``:

  ``COMPLETE``            — authority verified; mutation allowed.
  ``BLOCKED``             — plan validation failed; never ready.
  ``STALE_SOURCE``        — source revision changed since planning.
  ``LEASE_LOST``          — lease/fencing mismatch; another worker may hold it.
  ``AWAITING_DECISION``   — ambiguity gate: a blocker requires human decision.
  ``AUTHORITY_EXPIRED``   — authority TTL elapsed; replanning required.

``execute_operator()`` remains opt-in via ``SIMPLICIO_REQUIRE_MUTATION_AUTHORITY``
to avoid breaking existing run fixtures; the intent is to flip the default to
mandatory after the test suite is updated.
"""
from __future__ import annotations

import hashlib
import json
import time
import calendar
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

PLANNING_RECEIPT_SCHEMA = "simplicio.planning-receipt/v1"
RECEIPT_FILENAME = "planning-receipt.json"

# Verdict codes (also exported via intake_contract for shared use)
VERDICT_COMPLETE = "COMPLETE"
VERDICT_BLOCKED = "BLOCKED"
VERDICT_STALE_SOURCE = "STALE_SOURCE"
VERDICT_LEASE_LOST = "LEASE_LOST"
VERDICT_AWAITING_DECISION = "AWAITING_DECISION"
VERDICT_AUTHORITY_EXPIRED = "AUTHORITY_EXPIRED"

# Default authority TTL in seconds (30 minutes; callers may override).
DEFAULT_AUTHORITY_TTL_SECONDS = 1800


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(obj: Any) -> str:
    """Deterministic sha256 over a JSON-serializable structure."""
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def mutation_authority_token(*, run_id: str, attempt: int, task_contract_hash: str,
                            plan_hash: str, lease_id: str = "", fencing_token: str = "",
                            source_revision: str = "") -> str:
    """Derive the mutation-authority token from the exact identity tuple it authorizes.

    Any change to run/attempt/contract/plan/lease/fence/source_revision changes
    the token, so an authority minted for one identity can never silently validate
    a different one.  ``source_revision`` should be the immutable revision of the
    work-item source (e.g. GitHub issue ``updated_at``) so that a source change
    after planning invalidates the authority (``STALE_SOURCE``).
    """
    payload = {
        "run_id": str(run_id or ""),
        "attempt": int(attempt or 0),
        "task_contract_hash": str(task_contract_hash or ""),
        "plan_hash": str(plan_hash or ""),
        "lease_id": str(lease_id or ""),
        "fencing_token": str(fencing_token or ""),
        "source_revision": str(source_revision or ""),
    }
    return content_hash(payload)


def verify_mutation_authority(authority: str, *, run_id: str, attempt: int,
                              task_contract_hash: str, plan_hash: str,
                              lease_id: str = "", fencing_token: str = "",
                              source_revision: str = "") -> bool:
    """Fail-closed re-check: recompute the token from the CURRENT identity and compare."""
    if not str(authority or "").strip():
        return False
    expected = mutation_authority_token(
        run_id=run_id, attempt=attempt, task_contract_hash=task_contract_hash,
        plan_hash=plan_hash, lease_id=lease_id, fencing_token=fencing_token,
        source_revision=source_revision,
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
    source_revision: str = "",
    source_snapshot: Mapping[str, Any] | None = None,
    awaiting_decision: bool = False,
    awaiting_reason: str = "",
    authority_ttl_seconds: int = DEFAULT_AUTHORITY_TTL_SECONDS,
) -> Dict[str, Any]:
    """Build the #284 planning receipt from already-computed intake artifacts.

    ``plan_validation`` is the dict returned by ``plan_contract.validate_plan()``
    — this function does not re-derive plan/AC coverage, it packages the verdict
    already produced by the existing, tested validator into one hash-bound,
    immutable-once-ready receipt.

    New in #284:

    - ``source_revision`` — the revision of the work-item source (e.g. GitHub
      issue ``updated_at``); embedded in the authority token so that a source
      change after planning invalidates the authority (``STALE_SOURCE``).
    - ``awaiting_decision`` / ``awaiting_reason`` — if set, the receipt is
      marked ``AWAITING_DECISION`` and ``ready_for_mutation=False`` even when
      the plan is technically valid.
    - ``authority_ttl_seconds`` — when > 0, the receipt carries an
      ``authority_expires_at`` timestamp; ``evaluate_mutation_authority`` will
      return ``AUTHORITY_EXPIRED`` after the TTL elapses.
    """
    task_contract_hash = str(contract.get("collection_hash") or content_hash(contract))
    plan_hash = content_hash(plan)
    plan_valid = bool(plan_validation.get("valid"))

    if awaiting_decision:
        verdict = VERDICT_AWAITING_DECISION
        ready = False
    elif not plan_valid:
        verdict = VERDICT_BLOCKED
        ready = False
    else:
        verdict = VERDICT_COMPLETE
        ready = True

    authority = ""
    authority_expires_at = ""
    if ready:
        authority = mutation_authority_token(
            run_id=run_id, attempt=attempt, task_contract_hash=task_contract_hash,
            plan_hash=plan_hash, lease_id=lease_id, fencing_token=fencing_token,
            source_revision=source_revision,
        )
        if authority_ttl_seconds > 0:
            expires_ts = time.time() + authority_ttl_seconds
            authority_expires_at = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_ts)
            )

    return {
        "schema": PLANNING_RECEIPT_SCHEMA,
        "run_id": str(run_id or ""),
        "attempt": int(attempt or 0),
        "lease_id": str(lease_id or ""),
        "fencing_token": str(fencing_token or ""),
        "source_revision": str(source_revision or ""),
        "source_snapshot": dict(source_snapshot) if source_snapshot else {},
        "task_contract_hash": task_contract_hash,
        "plan_hash": plan_hash,
        "plan_validation": {
            "valid": plan_valid,
            "errors": list(plan_validation.get("errors") or []),
            "warnings": list(plan_validation.get("warnings") or []),
            "checked_tasks": plan_validation.get("checked_tasks", 0),
        },
        "awaiting_decision": bool(awaiting_decision),
        "awaiting_reason": str(awaiting_reason or ""),
        "verdict": verdict,
        "ready_for_mutation": ready,
        "mutation_authority": authority,
        "authority_expires_at": authority_expires_at,
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
    source_revision: str = "",
    current_source_revision: str = "",
) -> Dict[str, Any]:
    """Fail-closed gate: load the on-disk receipt and re-verify its authority
    against the CURRENT identity tuple the caller is about to execute with.

    Returns a structured verdict (never raises) so callers can render a clear
    BLOCKED reason instead of a bare exception.

    New verdict codes (#284):

    ``STALE_SOURCE``       — ``current_source_revision`` differs from the
                             revision recorded at planning time.
    ``LEASE_LOST``         — lease_id or fencing_token changed; another worker
                             may hold the item.
    ``AWAITING_DECISION``  — the receipt itself declared an ambiguity blocker.
    ``AUTHORITY_EXPIRED``  — the authority TTL elapsed; replanning required.
    """
    receipt = load_planning_receipt(run_dir)
    if not receipt:
        return {"ok": False, "verdict": VERDICT_BLOCKED,
                "reason_code": "planning_receipt_missing",
                "reason": f"{RECEIPT_FILENAME} is missing or unreadable"}
    if receipt.get("schema") != PLANNING_RECEIPT_SCHEMA:
        return {"ok": False, "verdict": VERDICT_BLOCKED,
                "reason_code": "planning_receipt_schema_invalid",
                "reason": "planning receipt schema mismatch"}

    # AWAITING_DECISION gate (checked before ready_for_mutation so the reason is clear)
    if receipt.get("awaiting_decision"):
        return {"ok": False, "verdict": VERDICT_AWAITING_DECISION,
                "reason_code": "planning_awaiting_decision",
                "reason": str(receipt.get("awaiting_reason") or "planning blocked — awaiting human decision")}

    if not receipt.get("ready_for_mutation"):
        return {"ok": False, "verdict": VERDICT_BLOCKED,
                "reason_code": "planning_not_ready",
                "reason": "planning receipt is not ready_for_mutation"}

    # AUTHORITY_EXPIRED gate
    expires_at = str(receipt.get("authority_expires_at") or "")
    if expires_at:
        try:
            # Use calendar.timegm so the UTC string is parsed correctly regardless
            # of the local timezone (time.mktime would interpret as local).
            expiry_ts = calendar.timegm(time.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ"))
            if time.time() > expiry_ts:
                return {"ok": False, "verdict": VERDICT_AUTHORITY_EXPIRED,
                        "reason_code": "authority_expired",
                        "reason": f"mutation authority expired at {expires_at}; replanning required"}
        except (ValueError, OverflowError):
            pass  # malformed timestamp — don't block on our own formatting error

    # STALE_SOURCE gate
    if current_source_revision:
        recorded_revision = str(receipt.get("source_revision") or "")
        if recorded_revision and recorded_revision != current_source_revision:
            return {"ok": False, "verdict": VERDICT_STALE_SOURCE,
                    "reason_code": "source_revision_changed",
                    "reason": (
                        f"source revision changed since planning "
                        f"(recorded={recorded_revision!r}, current={current_source_revision!r}); "
                        "replanning required"
                    )}

    authority = str(receipt.get("mutation_authority") or "")
    if not verify_mutation_authority(
        authority, run_id=run_id, attempt=attempt, task_contract_hash=task_contract_hash,
        plan_hash=plan_hash, lease_id=lease_id, fencing_token=fencing_token,
        source_revision=source_revision,
    ):
        # Determine whether the mismatch looks like a lease rotation
        receipt_lease = str(receipt.get("lease_id") or "")
        receipt_fence = str(receipt.get("fencing_token") or "")
        if (receipt_lease and receipt_lease != lease_id) or \
                (receipt_fence and receipt_fence != fencing_token):
            return {"ok": False, "verdict": VERDICT_LEASE_LOST,
                    "reason_code": "lease_or_fence_mismatch",
                    "reason": "lease_id or fencing_token changed since planning; another worker may hold this item"}
        return {"ok": False, "verdict": VERDICT_BLOCKED,
                "reason_code": "mutation_authority_invalid",
                "reason": "mutation authority does not match the current run/attempt/contract/plan/lease/fence/source identity"}

    return {"ok": True, "verdict": VERDICT_COMPLETE,
            "reason_code": "mutation_authority_verified",
            "reason": "mutation authority verified for the current identity tuple"}


__all__ = [
    "PLANNING_RECEIPT_SCHEMA",
    "RECEIPT_FILENAME",
    "VERDICT_COMPLETE",
    "VERDICT_BLOCKED",
    "VERDICT_STALE_SOURCE",
    "VERDICT_LEASE_LOST",
    "VERDICT_AWAITING_DECISION",
    "VERDICT_AUTHORITY_EXPIRED",
    "DEFAULT_AUTHORITY_TTL_SECONDS",
    "content_hash",
    "mutation_authority_token",
    "verify_mutation_authority",
    "build_planning_receipt",
    "receipt_path",
    "load_planning_receipt",
    "evaluate_mutation_authority",
]
