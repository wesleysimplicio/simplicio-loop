"""Cross-cutting production Loop-to-Runtime evidence gate (#696)."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .execution_route import verify_route_hash

SCHEMA = "simplicio.loop-runtime-production/v1"
REQUIRED = ("route", "effect", "context", "sessions", "installed")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def evaluate_production_integration(evidence: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate child receipts without turning partial evidence into success."""
    missing = [name for name in REQUIRED if name not in evidence]
    reasons = ["missing:" + name for name in missing]
    checks: dict[str, Any] = {}
    if "route" in evidence:
        checks["route"] = verify_route_hash(evidence["route"])
        if not checks["route"]:
            reasons.append("route_hash_invalid")
    if "effect" in evidence:
        effect = evidence["effect"]
        checks["effect"] = effect.get("profile") == "runtime-backed" and bool(effect.get("idempotency_key")) and bool(effect.get("lease_id"))
        if not checks["effect"]:
            reasons.append("effect_not_runtime_authoritative")
    for name in ("context", "sessions", "installed"):
        if name in evidence:
            checks[name] = evidence[name].get("status") in {"READY", "MEASURED"}
            if not checks[name]:
                reasons.append(name + "_not_ready")
    ready = not reasons and all(checks.values())
    body = {"schema": SCHEMA, "status": "READY" if ready else "BLOCKED",
            "effects_allowed": ready, "checks": checks, "reasons": sorted(set(reasons))}
    body["integration_hash"] = _hash(body)
    return body


__all__ = ["REQUIRED", "SCHEMA", "evaluate_production_integration"]
