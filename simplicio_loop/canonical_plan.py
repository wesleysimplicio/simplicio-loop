"""Consumer boundary for the Dev CLI canonical PlanDAG contract.

The Loop is a consumer of ``simplicio.plan-dag/v1``.  It must validate the
payload produced by Dev CLI and carry its digest into every effect dispatch;
it must not rebuild a plan from task prose or silently accept a different
schema major.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

CANONICAL_PLAN_SCHEMA = "simplicio.plan-dag/v1"
CANONICAL_PLAN_CONSUMER = "simplicio-loop"
PLAN_DIGEST_ALGORITHM = "sha256:canonical-json-sort-keys-compact"


class CanonicalPlanError(ValueError):
    """Raised when a canonical plan cannot be admitted by the Loop."""


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_dev_cli_models() -> tuple[Any, Any]:
    try:
        from simplicio.plan_compiler import PlanDAG, PlanValidationError
    except ImportError as exc:  # pragma: no cover - exercised by install probes
        raise CanonicalPlanError(
            "simplicio-dev-cli with plan_compiler support is required to consume "
            "simplicio.plan-dag/v1"
        ) from exc
    return PlanDAG, PlanValidationError


@dataclass(frozen=True)
class CanonicalPlan:
    """Validated immutable metadata for one canonical PlanDAG payload."""

    payload: dict[str, Any]
    digest: str

    @property
    def plan_id(self) -> str:
        return str(self.payload["plan_id"])

    @property
    def goal_id(self) -> str:
        return str(self.payload["goal_id"])

    @property
    def revision(self) -> str:
        return str(self.payload["revision"])

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


def load_canonical_plan(payload: Mapping[str, Any], *, expected_digest: str = "") -> CanonicalPlan:
    """Validate a Dev CLI PlanDAG and return its source digest.

    Validation is deliberately delegated to the Dev CLI's typed contract.  A
    Loop installation without that dependency fails closed instead of falling
    back to its legacy task-text planner.
    """
    if not isinstance(payload, Mapping):
        raise CanonicalPlanError("canonical PlanDAG payload must be an object")
    if payload.get("schema") != CANONICAL_PLAN_SCHEMA:
        raise CanonicalPlanError(
            f"unsupported canonical plan schema {payload.get('schema')!r}; "
            f"expected {CANONICAL_PLAN_SCHEMA!r}"
        )
    raw = dict(payload)
    PlanDAG, validation_error = _load_dev_cli_models()
    try:
        plan = PlanDAG.from_dict(raw)
        plan.validate()
    except (KeyError, TypeError, ValueError, validation_error) as exc:
        diagnostics = getattr(exc, "diagnostics", None)
        detail = "; ".join(str(item) for item in diagnostics) if diagnostics else str(exc)
        raise CanonicalPlanError(f"canonical PlanDAG rejected: {detail}") from exc
    normalized = plan.to_dict()
    digest = _digest(normalized)
    if expected_digest and expected_digest not in {digest, f"sha256:{digest}"}:
        raise CanonicalPlanError("canonical PlanDAG digest does not match expected_digest")
    return CanonicalPlan(payload=normalized, digest=digest)


def canonical_plan_metadata(plan: CanonicalPlan) -> dict[str, Any]:
    """Return the small causal envelope safe to attach to an effect request."""
    return {
        "schema": CANONICAL_PLAN_SCHEMA,
        "consumer": CANONICAL_PLAN_CONSUMER,
        "digest_algorithm": PLAN_DIGEST_ALGORITHM,
        "plan_id": plan.plan_id,
        "goal_id": plan.goal_id,
        "revision": plan.revision,
        "plan_digest": plan.digest,
    }


__all__ = [
    "CANONICAL_PLAN_CONSUMER",
    "CANONICAL_PLAN_SCHEMA",
    "CanonicalPlan",
    "CanonicalPlanError",
    "canonical_plan_metadata",
    "load_canonical_plan",
]
