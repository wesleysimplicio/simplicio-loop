"""Canonical, deterministic RouteDecision for Prompt enrichment.

The portable Loop route is the semantic source of truth for intent. Runtime may
add an available route and provenance, but it cannot grant write/effect
authority or replace the portable read-vs-mutate classification.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

ROUTE_SCHEMA = "simplicio.route-decision/v1"
POLICY_VERSION = "simplicio-loop-route-policy/v1"
AUTHORITY_LOCKED = {"writes": False, "effects": False}

MAX_SKILLS = 64
MAX_BYTES = 256 * 1024
ALLOWED_INTENTS = frozenset({"govern", "orchestrate", "validate", "mutate", "retrieve", "survey"})
ALLOWED_LANES = frozenset({"batch", "standard", "interactive"})
ALLOWED_RUNTIME_STATUSES = frozenset({"available", "unavailable", "incompatible"})
_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class RouteDecisionError(ValueError):
    """Raised when a canonical RouteDecision cannot be materialized."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_handles(values: Any, max_skills: int) -> list[str]:
    if not isinstance(values, list):
        return []
    handles: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        handle = value.strip()
        if not handle or not _HANDLE_RE.fullmatch(handle) or handle in seen:
            continue
        seen.add(handle)
        handles.append(handle)
        if len(handles) >= max_skills:
            break
    return handles


def _bounded_positive(value: Any, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if 0 < parsed <= maximum else default


def _lane(intent: str) -> str:
    if intent == "orchestrate":
        return "batch"
    if intent in {"mutate", "validate", "govern"}:
        return "standard"
    return "interactive"


def _decision_id(decision: Mapping[str, Any]) -> str:
    identity = {
        key: decision[key]
        for key in (
            "schema",
            "policy_version",
            "intent",
            "lane",
            "reason",
            "capability",
            "selected_handles",
            "max_skills",
            "max_bytes",
            "runtime_status",
            "authority",
            "provenance",
        )
        if key != "provenance"
    }
    # Provenance identifies the upstream producer, but a source transport or
    # Runtime-generated ID must not make the same semantic decision unstable.
    identity["portable_route_id"] = decision["provenance"].get("portable_route_id")
    identity["task_digest"] = decision["provenance"].get("task_digest")
    digest = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    return "loop-route/" + digest


def materialize_route_decision(
    task: str,
    portable_route: Mapping[str, Any],
    *,
    runtime_route: Mapping[str, Any] | None,
    selected_handles: list[str],
    max_skills: int,
    max_bytes: int,
    diagnostic: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one canonical RouteDecision without invoking a model/provider."""
    intent = str(portable_route.get("intent") or "survey").strip()
    if intent not in ALLOWED_INTENTS:
        raise RouteDecisionError(f"unsupported portable intent: {intent}")

    bounded_skills = _bounded_positive(max_skills, 1, MAX_SKILLS)
    bounded_bytes = _bounded_positive(max_bytes, 1_024, MAX_BYTES)
    source_route = runtime_route if isinstance(runtime_route, Mapping) else None
    diagnostic = diagnostic or {}
    healthy = source_route is not None
    runtime_status = "available" if healthy else str(diagnostic.get("runtime_status") or "unavailable")
    if runtime_status not in ALLOWED_RUNTIME_STATUSES:
        runtime_status = "incompatible" if "incompatible" in runtime_status else "unavailable"

    reason = "runtime_route_accepted" if healthy else str(
        diagnostic.get("reason_code") or "runtime_unavailable"
    )
    runtime_handles = source_route.get("selected_handles") if source_route else None
    # An available Runtime route can narrow handles, but an empty/omitted
    # Runtime list must not erase the portable enrichment selection.
    candidate_handles = runtime_handles or selected_handles
    handles = _canonical_handles(candidate_handles, bounded_skills)
    if source_route is not None:
        bounded_skills = min(
            bounded_skills,
            _bounded_positive(source_route.get("max_skills"), bounded_skills, MAX_SKILLS),
        )
        bounded_bytes = min(
            bounded_bytes,
            _bounded_positive(source_route.get("max_bytes"), bounded_bytes, MAX_BYTES),
        )
        handles = _canonical_handles(candidate_handles, bounded_skills)

    provenance: dict[str, Any] = {
        "producer": "simplicio-loop",
        "policy_version": POLICY_VERSION,
        "source": str(diagnostic.get("source") or ("runtime" if healthy else "fallback")),
        "portable_route_id": portable_route.get("route_id"),
        "task_digest": "sha256:" + hashlib.sha256(task.encode("utf-8")).hexdigest(),
    }
    if source_route is not None and str(source_route.get("decision_id") or "").strip():
        provenance["source_decision_id"] = str(source_route["decision_id"])
    if source_route is not None and str(source_route.get("reason") or "").strip():
        provenance["source_reason"] = str(source_route["reason"])

    decision: dict[str, Any] = {
        "schema": ROUTE_SCHEMA,
        "policy_version": POLICY_VERSION,
        "decision_id": "",
        # Intent comes from the portable router so read and mutation prompts
        # cannot be collapsed by an inconsistent upstream Runtime payload.
        "intent": intent,
        "lane": _lane(intent),
        "reason": reason,
        "capability": "prompt.enrich",
        "selected_handles": handles,
        "max_skills": bounded_skills,
        "max_bytes": bounded_bytes,
        "runtime_status": runtime_status,
        "authority": dict(AUTHORITY_LOCKED),
        "provenance": provenance,
    }
    decision["decision_id"] = _decision_id(decision)
    validate_route_decision(decision)
    return decision


def validate_route_decision(value: Mapping[str, Any]) -> None:
    """Validate the in-process invariants of a canonical RouteDecision."""
    if not isinstance(value, Mapping):
        raise RouteDecisionError("route decision must be an object")
    required = {
        "schema", "policy_version", "decision_id", "intent", "lane", "reason",
        "capability", "selected_handles", "max_skills", "max_bytes",
        "runtime_status", "authority", "provenance",
    }
    missing = sorted(required.difference(value))
    if missing:
        raise RouteDecisionError("missing fields: " + ", ".join(missing))
    if value["schema"] != ROUTE_SCHEMA or value["policy_version"] != POLICY_VERSION:
        raise RouteDecisionError("route decision schema or policy is incompatible")
    if value["intent"] not in ALLOWED_INTENTS or value["lane"] not in ALLOWED_LANES:
        raise RouteDecisionError("route decision intent/lane is invalid")
    if value["lane"] != _lane(str(value["intent"])):
        raise RouteDecisionError("route decision lane does not match intent")
    if value["capability"] != "prompt.enrich" or not str(value["reason"]).strip():
        raise RouteDecisionError("route decision capability/reason is invalid")
    if value["runtime_status"] not in ALLOWED_RUNTIME_STATUSES:
        raise RouteDecisionError("route decision runtime_status is invalid")
    if not isinstance(value["decision_id"], str) or not value["decision_id"].startswith("loop-route/"):
        raise RouteDecisionError("route decision id is invalid")
    if not isinstance(value["selected_handles"], list) or any(
        not isinstance(handle, str) or not _HANDLE_RE.fullmatch(handle)
        for handle in value["selected_handles"]
    ):
        raise RouteDecisionError("route decision handles are invalid")
    if not isinstance(value["max_skills"], int) or not 0 < value["max_skills"] <= MAX_SKILLS:
        raise RouteDecisionError("route decision max_skills is invalid")
    if not isinstance(value["max_bytes"], int) or not 0 < value["max_bytes"] <= MAX_BYTES:
        raise RouteDecisionError("route decision max_bytes is invalid")
    if value["authority"] != AUTHORITY_LOCKED:
        raise RouteDecisionError("route decision authority is not locked")
    provenance = value["provenance"]
    if not isinstance(provenance, Mapping) or provenance.get("producer") != "simplicio-loop":
        raise RouteDecisionError("route decision provenance is invalid")
    if _decision_id(value) != value["decision_id"]:
        raise RouteDecisionError("route decision id is not deterministic")


__all__ = [
    "ALLOWED_INTENTS",
    "ALLOWED_LANES",
    "ALLOWED_RUNTIME_STATUSES",
    "AUTHORITY_LOCKED",
    "POLICY_VERSION",
    "ROUTE_SCHEMA",
    "RouteDecisionError",
    "materialize_route_decision",
    "validate_route_decision",
]
