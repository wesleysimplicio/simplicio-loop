"""Deterministic runtime/model router (issue #287, slice 1 of the EPIC).

Pairs with ``simplicio_loop/model_registry.py``. Given task requirements (role,
required/preferred capabilities, provider allow/deny lists, an
``independent_review`` constraint) and a ``ModelCapabilityRegistry``, produces a
``routing-decision-receipt`` - every candidate considered, its score and
accept/reject reason, the final selection (or a diagnosed block), a
``registry_hash``, ``policy_version`` and timestamp.

Determinism is the whole point: the same requirements + registry always produce
byte-identical receipts (modulo timestamp), and reordering the registry's input
entries never changes which candidate wins - candidates are ranked by score then
by an explicit, content-derived tiebreak key ``(runtime, provider, model_id)``,
never by dict/set/list iteration order.

Out of scope here (left for later slices of #287): real Codex/Claude
``RuntimeDriver`` execution, fallback/circuit-breaker semantics, and scheduler
integration - this module only decides, it never launches anything.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .model_registry import ModelCapabilityRegistry

SCHEMA = "simplicio.routing-decision-receipt/v1"
POLICY_VERSION = "1"
ROLES = frozenset(("planner", "executor", "reviewer", "tester"))


class ModelRouterError(ValueError):
    """Raised for malformed routing requirements."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _text(value: Any) -> str:
    return str(value or "").strip()


def _str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        raise ModelRouterError("expected a list of strings, got a bare string")
    return sorted({str(item).strip() for item in value if str(item).strip()})


def _normalize_requirements(requirements: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(requirements, Mapping):
        raise ModelRouterError("requirements must be an object")
    role = _text(requirements.get("role"))
    if role not in ROLES:
        raise ModelRouterError(f"role must be one of {sorted(ROLES)}, got {role!r}")
    return {
        "role": role,
        "required_capabilities": _str_list(requirements.get("required_capabilities")),
        "preferred_capabilities": _str_list(requirements.get("preferred_capabilities")),
        "allowed_providers": _str_list(requirements.get("allowed_providers")),
        "denied_providers": _str_list(requirements.get("denied_providers")),
        "os": _text(requirements.get("os")),
        "arch": _text(requirements.get("arch")),
        "context_window_min": int(requirements.get("context_window_min") or 0),
        "require_probe_available": bool(requirements.get("require_probe_available", True)),
        "independent_review": bool(requirements.get("independent_review", False)),
    }


def _score(entry: Mapping[str, Any], preferred_capabilities: Sequence[str]) -> int:
    if not preferred_capabilities:
        return 0
    have = set(entry.get("capabilities") or [])
    return sum(1 for cap in preferred_capabilities if cap in have)


def _tiebreak_key(entry: Mapping[str, Any]):
    # Content-derived, never dependent on dict/list iteration order.
    return (entry["runtime"], entry["provider"], entry["model_id"])


def route(requirements: Mapping[str, Any], registry: ModelCapabilityRegistry, *,
          executor_route: Optional[Mapping[str, Any]] = None,
          policy_version: str = POLICY_VERSION) -> Dict[str, Any]:
    """Resolve one routing decision and return its receipt (never a silent default).

    ``executor_route`` should be the previously-selected ``{"runtime", "provider",
    "model_id"}`` for the executor role, when routing a ``reviewer``/``tester``
    with ``independent_review=True``: a candidate matching that route is rejected
    with reason code ``policy_denied`` rather than silently allowed through.
    """
    normalized = _normalize_requirements(requirements)
    filtered = registry.eligible_candidates(normalized)
    eliminated = filtered["eliminated"]
    eligible = filtered["eligible"]

    executor_key = None
    if normalized["independent_review"] and executor_route:
        executor_key = (
            _text(executor_route.get("runtime")),
            _text(executor_route.get("provider")),
            _text(executor_route.get("model_id")),
        )

    scored: List[Dict[str, Any]] = []
    policy_rejected: List[Dict[str, Any]] = []
    for entry in eligible:
        key = _tiebreak_key(entry)
        if executor_key is not None and key == executor_key:
            policy_rejected.append({
                "runtime": entry["runtime"], "provider": entry["provider"], "model_id": entry["model_id"],
                "score": _score(entry, normalized["preferred_capabilities"]),
                "reason_code": "policy_denied",
            })
            continue
        scored.append({
            "runtime": entry["runtime"], "provider": entry["provider"], "model_id": entry["model_id"],
            "score": _score(entry, normalized["preferred_capabilities"]),
        })

    # Deterministic ranking: highest score first, ties broken by a stable,
    # content-derived key - independent of the order candidates were supplied in.
    scored.sort(key=lambda c: (-c["score"], c["runtime"], c["provider"], c["model_id"]))

    selected = scored[0] if scored else None
    blocked = selected is None
    block_reason = ""
    if blocked:
        if not eligible:
            block_reason = "no candidate satisfies mandatory requirements"
        else:
            block_reason = "all eligible candidates were rejected by policy (independent_review)"

    candidates: List[Dict[str, Any]] = []
    for item in eliminated:
        candidates.append({
            "runtime": item["runtime"], "provider": item["provider"], "model_id": item["model_id"],
            "score": None, "status": "rejected", "reason_code": item["reason_code"],
        })
    for item in policy_rejected:
        candidates.append({
            "runtime": item["runtime"], "provider": item["provider"], "model_id": item["model_id"],
            "score": item["score"], "status": "rejected", "reason_code": item["reason_code"],
        })
    for item in scored:
        status = "selected" if selected is not None and item is selected else "rejected"
        entry_dict = {
            "runtime": item["runtime"], "provider": item["provider"], "model_id": item["model_id"],
            "score": item["score"], "status": status,
        }
        if status == "rejected":
            entry_dict["reason_code"] = "policy_denied" if selected is not None else "capacity_exhausted"
        candidates.append(entry_dict)

    # Candidate list itself is also sorted by the content-derived key so the
    # receipt is byte-identical across re-orderings of the input registry.
    candidates.sort(key=lambda c: (c["runtime"], c["provider"], c["model_id"]))

    receipt = {
        "schema": SCHEMA,
        "policy_version": _text(policy_version) or POLICY_VERSION,
        "registry_hash": registry.registry_hash,
        "requirements": normalized,
        "candidates": candidates,
        "selected": (
            {"runtime": selected["runtime"], "provider": selected["provider"], "model_id": selected["model_id"],
             "score": selected["score"]}
            if selected is not None else None
        ),
        "blocked": blocked,
        "block_reason": block_reason,
        "timestamp": _now(),
    }
    return receipt


__all__ = ["ModelRouterError", "POLICY_VERSION", "ROLES", "SCHEMA", "route"]
