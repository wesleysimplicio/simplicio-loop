"""Evidence-driven model escalation and safe downgrade policy (#678).

This module decides where reasoning may run; it never grants mutation authority
and never performs a provider request. Safety constraints are evaluated before
quality preferences and a JSON receipt records the decision and budget state.
"""
from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from typing import Any, Dict, Mapping, MutableMapping, Optional

SCHEMA = "simplicio.model-routing/v1"
POLICY_VERSION = "1"
DETERMINISTIC = "deterministic"
LOCAL = "local"
STRONG_LOCAL = "strong_local"
REMOTE = "remote"
BLOCKED = "blocked"
DOWNGRADE = "downgrade"
DECISIONS = frozenset((DETERMINISTIC, LOCAL, STRONG_LOCAL, REMOTE, BLOCKED, DOWNGRADE))


class RoutingPolicyError(ValueError):
    """Raised only for malformed, non-safety-critical input types."""


def _bool(value: Any, name: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise RoutingPolicyError(f"{name} must be boolean")
    return value


def _number(value: Any, name: str, default: float = 0.0, integer: bool = False) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RoutingPolicyError(f"{name} must be numeric")
    result = int(value) if integer else float(value)
    if result < 0:
        raise RoutingPolicyError(f"{name} must be non-negative")
    return result


def _text(value: Any, name: str, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise RoutingPolicyError(f"{name} must be a string")
    return value.strip()


def _stable_id(receipt: Mapping[str, Any]) -> str:
    body = {key: value for key, value in receipt.items() if key not in {"timestamp", "receipt_id"}}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _handoff(request: Mapping[str, Any]) -> Dict[str, Any]:
    raw = request.get("handoff") or {}
    if not isinstance(raw, Mapping):
        raise RoutingPolicyError("handoff must be an object")
    refs = raw.get("references") or []
    if isinstance(refs, (str, bytes)) or not isinstance(refs, (list, tuple)):
        raise RoutingPolicyError("handoff.references must be a list")
    references = [str(ref).strip() for ref in refs[:8] if str(ref).strip()]
    summary = raw.get("evidence_summary", "")
    if not isinstance(summary, str):
        raise RoutingPolicyError("handoff.evidence_summary must be a string")
    return {
        "references": references,
        "evidence_summary": summary[:1024],
        "redacted": True,
        "transcript_included": False,
    }


def _fallback(request: Mapping[str, Any], reasons: list[str], handoff: Dict[str, Any], now: float) -> str:
    if _bool(request.get("deterministic_capable"), "deterministic_capable"):
        reasons.append("deterministic_capable")
        return DETERMINISTIC
    if _bool(request.get("local_capable"), "local_capable"):
        reasons.append("remote_forbidden_local_fallback")
        return LOCAL
    if _bool(request.get("strong_local_capable"), "strong_local_capable"):
        reasons.append("strong_local_fallback")
        return STRONG_LOCAL
    reasons.append("no_safe_fallback")
    return BLOCKED


def evaluate(request: Mapping[str, Any], *, now: Optional[float] = None) -> Dict[str, Any]:
    """Return a deterministic, JSON-serializable routing receipt.

    Remote is opt-in and is considered only after privacy/offline/cost/provider
    gates. ``mutation_authority`` is deliberately informational: Runtime owns
    all effects regardless of the selected model.
    """
    if not isinstance(request, Mapping):
        raise RoutingPolicyError("request must be an object")
    now = time.time() if now is None else float(now)
    if now < 0:
        raise RoutingPolicyError("now must be non-negative")
    reasons: list[str] = []
    try:
        privacy = _bool(request.get("privacy_sensitive"), "privacy_sensitive")
        local_only = _bool(request.get("local_only"), "local_only")
        offline = _bool(request.get("offline"), "offline")
        remote_allowed = _bool(request.get("remote_allowed"), "remote_allowed")
        remote_available = _bool(request.get("remote_available"), "remote_available")
        deterministic_capable = _bool(request.get("deterministic_capable"), "deterministic_capable")
        local_capable = _bool(request.get("local_capable"), "local_capable")
        strong_local_capable = _bool(request.get("strong_local_capable"), "strong_local_capable")
        stall = _bool(request.get("stall_detected"), "stall_detected")
        invalid_syntax = _bool(request.get("invalid_tool_syntax"), "invalid_tool_syntax")
        higher_required = _bool(request.get("higher_capability_required"), "higher_capability_required")
        provider = _text(request.get("remote_provider"), "remote_provider")
        allowed_providers = request.get("allowed_remote_providers") or []
        if isinstance(allowed_providers, (str, bytes)) or not isinstance(allowed_providers, (list, tuple, set)):
            raise RoutingPolicyError("allowed_remote_providers must be a list")
        allowed_providers = {str(item).strip() for item in allowed_providers if str(item).strip()}
        max_cost = _number(request.get("max_cost_usd"), "max_cost_usd")
        estimated_cost = _number(request.get("estimated_remote_cost_usd"), "estimated_remote_cost_usd")
        max_elapsed = _number(request.get("max_elapsed_seconds"), "max_elapsed_seconds", 300.0)
        max_calls = _number(request.get("max_calls"), "max_calls", 4.0, integer=True)
        max_tokens = _number(request.get("max_tokens"), "max_tokens", 16000.0, integer=True)
        max_escalations = _number(request.get("max_escalations"), "max_escalations", 1.0, integer=True)
        syntax_repairs = _number(request.get("syntax_repairs"), "syntax_repairs", 0.0, integer=True)
        max_syntax_repairs = _number(request.get("max_syntax_repairs"), "max_syntax_repairs", 1.0, integer=True)
        escalation_count = _number(request.get("escalation_count"), "escalation_count", 0.0, integer=True)
        progress = _number(request.get("semantic_progress"), "semantic_progress", 1.0)
        if progress > 1:
            raise RoutingPolicyError("semantic_progress must be between 0 and 1")
        current = _text(request.get("current_tier"), "current_tier")
        cooldown_until = _number(request.get("cooldown_until"), "cooldown_until", 0.0)
        effects = request.get("completed_effect_ids") or []
        if isinstance(effects, (str, bytes)) or not isinstance(effects, (list, tuple, set)):
            raise RoutingPolicyError("completed_effect_ids must be a list")
        completed_effects = sorted({str(effect).strip() for effect in effects if str(effect).strip()})
        handoff = _handoff(request)
    except RoutingPolicyError:
        raise

    safety_blocks_remote = []
    if privacy:
        safety_blocks_remote.append("privacy_sensitive")
    if local_only:
        safety_blocks_remote.append("local_only")
    if offline:
        safety_blocks_remote.append("offline")
    if not remote_allowed:
        safety_blocks_remote.append("remote_not_allowed")
    if not remote_available:
        safety_blocks_remote.append("remote_unavailable")
    if allowed_providers and provider not in allowed_providers:
        safety_blocks_remote.append("remote_provider_not_allowlisted")
    if estimated_cost > max_cost:
        safety_blocks_remote.append("remote_cost_budget_exceeded")
    if max_calls < 1 or max_tokens < 1 or max_elapsed <= 0:
        safety_blocks_remote.append("budget_exhausted")
    if safety_blocks_remote:
        reasons.extend(safety_blocks_remote)

    needs_escalation = higher_required or stall or progress < 0.5
    if invalid_syntax and syntax_repairs < max_syntax_repairs:
        reasons.append("bounded_tool_syntax_repair")
        needs_escalation = False
    elif invalid_syntax:
        reasons.append("tool_syntax_repair_exhausted")
        needs_escalation = True

    if deterministic_capable and not needs_escalation:
        decision = DETERMINISTIC
        reasons.append("deterministic_first")
    elif local_capable and not needs_escalation:
        decision = LOCAL
        reasons.append("local_first")
    elif needs_escalation and cooldown_until > now and current in {LOCAL, STRONG_LOCAL}:
        decision = current
        reasons.append("escalation_cooldown")
    elif strong_local_capable and needs_escalation:
        decision = STRONG_LOCAL
        reasons.append("strong_local_after_evidence")
    elif needs_escalation and escalation_count < max_escalations and not safety_blocks_remote:
        decision = REMOTE
        reasons.append("measured_escalation")
    else:
        decision = _fallback(request, reasons, handoff, now)

    receipt: Dict[str, Any] = {
        "schema": SCHEMA,
        "policy_version": POLICY_VERSION,
        "timestamp": now,
        "decision": decision,
        "reason_codes": sorted(set(reasons)),
        "estimated_budget": {
            "max_calls": int(max_calls), "max_tokens": int(max_tokens),
            "max_elapsed_seconds": max_elapsed, "max_cost_usd": max_cost,
            "max_escalations": int(max_escalations),
        },
        "observed_usage": {"calls": 0, "tokens": 0, "elapsed_seconds": 0.0, "cost_usd": 0.0},
        "escalation_count": int(escalation_count + (decision == REMOTE)),
        "downgrade": False,
        "remote_request_allowed": decision == REMOTE,
        "completed_effect_ids": completed_effects,
        "handoff": handoff,
        "mutation_authority": "runtime",
        "mutation_authorized": False,
    }
    receipt["receipt_id"] = _stable_id(receipt)
    return receipt


def record_observation(receipt: Mapping[str, Any], *, calls: int, tokens: int,
                       elapsed_seconds: float, cost_usd: float = 0.0,
                       outcome: str = "") -> Dict[str, Any]:
    """Attach measured usage without changing the selected authority."""
    if not isinstance(receipt, Mapping):
        raise RoutingPolicyError("receipt must be an object")
    observed = {"calls": int(_number(calls, "calls", integer=True)),
                "tokens": int(_number(tokens, "tokens", integer=True)),
                "elapsed_seconds": _number(elapsed_seconds, "elapsed_seconds"),
                "cost_usd": _number(cost_usd, "cost_usd")}
    result = deepcopy(dict(receipt))
    result["observed_usage"] = observed
    result["outcome"] = _text(outcome, "outcome")
    result["receipt_id"] = _stable_id(result)
    return result


def maybe_downgrade(receipt: Mapping[str, Any], *, deterministic_ready: bool = True) -> Dict[str, Any]:
    """Return a new receipt for safe deterministic delivery/validation."""
    if not isinstance(receipt, Mapping):
        raise RoutingPolicyError("receipt must be an object")
    result = deepcopy(dict(receipt))
    result["downgrade"] = bool(deterministic_ready and result.get("decision") in {REMOTE, STRONG_LOCAL, LOCAL})
    if result["downgrade"]:
        result["decision"] = DOWNGRADE
        result["remote_request_allowed"] = False
        result.setdefault("reason_codes", []).append("deterministic_delivery_ready")
        result["reason_codes"] = sorted(set(result["reason_codes"]))
    result["receipt_id"] = _stable_id(result)
    return result


__all__ = ["BLOCKED", "DECISIONS", "DETERMINISTIC", "DOWNGRADE", "LOCAL", "POLICY_VERSION",
           "REMOTE", "RoutingPolicyError", "SCHEMA", "STRONG_LOCAL", "evaluate",
           "maybe_downgrade", "record_observation"]
