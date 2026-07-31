"""Bounded, safe recovery decisions for queue/drain blockers."""
from __future__ import annotations

from typing import Any, Mapping

RECOVERABLE = {
    "live_lock": ("diagnose_ownership", "bounded_wait"),
    "operator_failure": ("capability_check", "bounded_repair"),
    "provider_failure": ("capability_check", "bounded_repair"),
    "post_merge_residual": ("requery_source", "review_and_close"),
}


def recovery_stage(blocker: str, *, attempts: int = 0, max_attempts: int = 2) -> dict[str, Any]:
    """Return the next safe recovery action, or a final honest blocked handoff.

    This function only plans/records recovery. It never kills a process, removes a
    lock, starts a provider, bypasses an operator, or closes an external issue.
    """
    key = str(blocker or "").strip().lower()
    actions = RECOVERABLE.get(key)
    if not actions:
        return {"status": "BLOCKED", "reason_code": "external_authority_blocked",
                "attempts": attempts, "max_attempts": max_attempts,
                "tag": "UNVERIFIED"}
    if attempts < 0 or max_attempts < 1:
        raise ValueError("recovery attempt bounds must be positive")
    if attempts >= max_attempts:
        return {"status": "BLOCKED", "reason_code": f"{key}_recovery_exhausted",
                "attempts": attempts, "max_attempts": max_attempts,
                "allowed_actions": list(actions), "tag": "UNVERIFIED"}
    return {"status": "CONTINUE", "reason_code": f"{key}_recovery_pending",
            "attempt": attempts + 1, "max_attempts": max_attempts,
            "action": actions[attempts] if attempts < len(actions) else actions[-1],
            "allowed_actions": list(actions), "tag": "MEASURED"}


__all__ = ["RECOVERABLE", "recovery_stage"]
