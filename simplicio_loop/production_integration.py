"""Cross-cutting production Loop-to-Runtime evidence gate (#696)."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Dict, Mapping, Optional

from .execution_route import verify_route_hash
from .feedback_recovery_agent import reconcile_external_effect
from .installed_runtime_e2e import COMPONENTS
from .receipt_verifier import verify_receipt
from .runtime_execution_receipt import RUNTIME_EXECUTION_RECEIPT_SCHEMA

SCHEMA = "simplicio.loop-runtime-production/v1"
HARNESS_SCHEMA = "simplicio.loop-runtime-production-harness/v1"
REQUIRED = ("route", "effect", "context", "sessions", "installed")
HARNESS_REQUIRED = REQUIRED + ("execution", "reconciliation")


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


def _stage(verified: bool, reason: str = "") -> Dict[str, Any]:
    return {"status": "VERIFIED" if verified else "BLOCKED", "verified": verified, "reason": reason}


def _status_ready(value: Any) -> bool:
    return value in {"READY", "MEASURED", "VERIFIED"}


def _check_effect(effect: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(effect, Mapping):
        return _stage(False, "effect_missing")
    valid = (
        effect.get("profile") == "runtime-backed"
        and effect.get("executor") == "simplicio-runtime"
        and effect.get("status") in {"MEASURED", "READY"}
        and bool(effect.get("idempotency_key"))
        and bool(effect.get("lease_id"))
        and isinstance(effect.get("fencing_token"), int)
        and effect.get("fencing_token", 0) > 0
    )
    return _stage(valid, "" if valid else "effect_not_runtime_authoritative")


def _check_installed(installed: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(installed, Mapping):
        return _stage(False, "installed_missing")
    components = installed.get("components", {})
    component_ok = isinstance(components, Mapping) and set(components) == set(COMPONENTS) and all(
        isinstance(row, Mapping) and row.get("status") == "READY" for row in components.values()
    )
    correlations = {
        str(row.get("correlation_id")) for row in components.values()
        if isinstance(row, Mapping) and row.get("correlation_id")
    }
    correlation_ok = len(correlations) <= 1
    valid = (
        installed.get("status") == "READY"
        and installed.get("effects_attempted") is False
        and component_ok
        and correlation_ok
    )
    if valid:
        return _stage(True)
    if installed.get("status") != "READY":
        return _stage(False, "installed_not_ready")
    if installed.get("effects_attempted") is not False:
        return _stage(False, "installed_effects_attempted")
    return _stage(False, "installed_correlation_invalid")


def _check_execution(
    execution: Mapping[str, Any],
    *,
    route_id: str,
    installed: Mapping[str, Any],
    max_age_seconds: Optional[float],
) -> Dict[str, Any]:
    if not isinstance(execution, Mapping):
        return _stage(False, "execution_missing")
    if not execution.get("receipt_sha") or not isinstance(execution.get("evidence_refs"), list):
        return _stage(False, "execution_receipt_invalid_schema")
    verdict = verify_receipt(
        execution,
        schema=RUNTIME_EXECUTION_RECEIPT_SCHEMA,
        max_age_seconds=max_age_seconds,
    )
    if not verdict.verified:
        return _stage(False, "execution_receipt_" + verdict.status.lower())
    if execution.get("route_id") != route_id:
        return _stage(False, "execution_route_mismatch")
    refs = set(execution.get("evidence_refs") or ())
    installed_refs = {str(installed.get("report_hash") or ""), str(installed.get("correlation_id") or "")}
    installed_refs.discard("")
    if not installed_refs.intersection(refs):
        return _stage(False, "execution_installed_evidence_missing")
    return _stage(True)


def _check_reconciliation(
    effect: Mapping[str, Any],
    observed: Optional[Mapping[str, Any]],
    expected_intent: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    intent_id = str((expected_intent or {}).get("intent_id") or effect.get("idempotency_key") or "")
    verdict = reconcile_external_effect(
        observed_state=observed,
        expected_intent={"intent_id": intent_id},
    )
    verified = verdict.get("reconciled") is True and verdict.get("outcome") == "succeeded"
    return {
        "status": "VERIFIED" if verified else "BLOCKED",
        "verified": verified,
        "reason": "" if verified else "reconciliation_" + str(verdict.get("outcome") or "blocked"),
        **verdict,
    }


class ProductionIntegrationHarness:
    """Run and reconcile the production-shaped Loop-to-Runtime evidence chain."""

    def __init__(
        self,
        evidence: Mapping[str, Mapping[str, Any]],
        *,
        execute: Optional[Callable[[Mapping[str, Any]], Mapping[str, Any]]] = None,
        reconcile: Optional[Callable[[Mapping[str, Any]], Optional[Mapping[str, Any]]]] = None,
        expected_intent: Optional[Mapping[str, Any]] = None,
        max_age_seconds: Optional[float] = None,
    ) -> None:
        self.evidence = dict(evidence)
        self.execute = execute
        self.reconcile = reconcile
        self.expected_intent = dict(expected_intent or {})
        self.max_age_seconds = max_age_seconds

    def run(self) -> Dict[str, Any]:
        evidence = dict(self.evidence)
        route = evidence.get("route")
        effect = evidence.get("effect")
        installed = evidence.get("installed")
        route_id = str(route.get("receipt_sha") or route.get("route_id") or "") if isinstance(route, Mapping) else ""
        execution = evidence.get("execution")
        callback_reasons = []
        if self.execute is not None:
            try:
                execution = self.execute({
                    "schema": HARNESS_SCHEMA,
                    "route": route,
                    "effect": effect,
                    "context": evidence.get("context"),
                    "sessions": evidence.get("sessions"),
                    "installed": installed,
                })
            except Exception as exc:
                execution = None
                callback_reasons.append("execution_callback_" + type(exc).__name__)

        observed = evidence.get("observed_effect")
        intent = dict(self.expected_intent)
        if not intent and isinstance(effect, Mapping):
            intent = {"intent_id": effect.get("idempotency_key")}
        if self.reconcile is not None:
            try:
                observed = self.reconcile(intent)
            except Exception as exc:
                observed = None
                callback_reasons.append("reconciliation_callback_" + type(exc).__name__)

        checks: Dict[str, Any] = {}
        route_valid = isinstance(route, Mapping) and bool(route_id) and verify_route_hash(route)
        checks["route"] = _stage(route_valid, "" if route_valid else "route_hash_invalid")
        checks["effect"] = _check_effect(effect if isinstance(effect, Mapping) else {})
        for name in ("context", "sessions"):
            value = evidence.get(name)
            checks[name] = _stage(_status_ready(value.get("status")) if isinstance(value, Mapping) else False,
                                  "" if isinstance(value, Mapping) and _status_ready(value.get("status")) else name + "_not_ready")
        checks["installed"] = _check_installed(installed if isinstance(installed, Mapping) else {})
        checks["execution"] = _check_execution(
            execution if isinstance(execution, Mapping) else {},
            route_id=route_id,
            installed=installed if isinstance(installed, Mapping) else {},
            max_age_seconds=self.max_age_seconds,
        )
        checks["reconciliation"] = _check_reconciliation(
            effect if isinstance(effect, Mapping) else {}, observed, intent,
        )
        reasons = list(callback_reasons)
        reasons.extend(check["reason"] for check in checks.values() if check.get("reason"))
        reasons = sorted(set(reasons))
        ready = not reasons and all(check.get("verified") is True for check in checks.values())
        report: Dict[str, Any] = {
            "schema": HARNESS_SCHEMA,
            "status": "READY" if ready else "BLOCKED",
            "effects_allowed": ready,
            "checks": checks,
            "reasons": reasons,
            "route_id": route_id,
            "intent_id": str(intent.get("intent_id") or ""),
        }
        report["integration_hash"] = _hash(report)
        return report


def run_production_integration_harness(
    evidence: Mapping[str, Mapping[str, Any]],
    *,
    execute: Optional[Callable[[Mapping[str, Any]], Mapping[str, Any]]] = None,
    reconcile: Optional[Callable[[Mapping[str, Any]], Optional[Mapping[str, Any]]]] = None,
    expected_intent: Optional[Mapping[str, Any]] = None,
    max_age_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    return ProductionIntegrationHarness(
        evidence,
        execute=execute,
        reconcile=reconcile,
        expected_intent=expected_intent,
        max_age_seconds=max_age_seconds,
    ).run()


__all__ = [
    "HARNESS_REQUIRED",
    "HARNESS_SCHEMA",
    "ProductionIntegrationHarness",
    "REQUIRED",
    "SCHEMA",
    "evaluate_production_integration",
    "run_production_integration_harness",
]
