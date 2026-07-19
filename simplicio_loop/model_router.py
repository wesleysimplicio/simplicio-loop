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

This slice adds fallback/circuit-breaker semantics: ``classify_failure``/
``fallback_decision`` turn a failure reason code into retry-same-route /
fallback-route / terminal, ``compute_backoff`` gives a deterministic (seedable)
backoff+jitter delay, ``CircuitBreaker`` tracks per-(runtime, provider, model_id)
failure counts and opens/half-opens/closes on a caller-supplied clock, and
``route_with_fallback`` wires all three into one receipt linked to the failed
route by ``previous_route_id`` -- never silently swapping the runtime mid-attempt.

Out of scope here (left for later slices of #287): real Codex/Claude
``RuntimeDriver`` execution and scheduler integration - this module only
decides, it never launches anything.
"""
from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .model_registry import REASON_CODES, ModelCapabilityRegistry

SCHEMA = "simplicio.routing-decision-receipt/v1"
POLICY_VERSION = "1"
ROLES = frozenset(("planner", "executor", "reviewer", "tester"))

# Failure-reason vocabulary for the fallback layer. Deliberately broader than
# model_registry.REASON_CODES (which describes *why a candidate was ineligible*
# at routing time) because these describe *why an in-flight execution failed*
# -- e.g. "timeout"/"rate_limited" only make sense after a route was already
# selected and dispatched.
FAILURE_REASON_CLASSES: Dict[str, str] = {
    "timeout": "transient",
    "rate_limited": "transient",
    "runtime_unavailable": "transient",
    "capacity_exhausted": "transient",
    "internal_error": "transient",
    "auth_missing": "permanent",
    "policy_denied": "permanent",
    "context_limit": "permanent",
    "budget_exceeded": "permanent",
    "device_incompatible": "permanent",
    "invalid_output": "permanent",
}
FAILURE_REASON_CODES = frozenset(FAILURE_REASON_CLASSES)
FALLBACK_DECISIONS = frozenset(("retry_same_route", "fallback_route", "terminal"))


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
          policy_version: str = POLICY_VERSION,
          policy_rejections: Optional[Mapping[Tuple[str, str, str], str]] = None) -> Dict[str, Any]:
    """Resolve one routing decision and return its receipt (never a silent default).

    ``executor_route`` should be the previously-selected ``{"runtime", "provider",
    "model_id"}`` for the executor role, when routing a ``reviewer``/``tester``
    with ``independent_review=True``: a candidate matching that route is rejected
    with reason code ``policy_denied`` rather than silently allowed through.

    ``policy_rejections`` (used by :func:`route_with_fallback`) maps a
    ``(runtime, provider, model_id)`` key to the reason code it should be rejected
    with -- a general mechanism for excluding specific candidates (e.g. a route
    whose circuit breaker is open) without touching registry eligibility.
    """
    normalized = _normalize_requirements(requirements)
    filtered = registry.eligible_candidates(normalized)
    eliminated = filtered["eliminated"]
    eligible = filtered["eligible"]

    rejections: Dict[Tuple[str, str, str], str] = dict(policy_rejections or {})
    if normalized["independent_review"] and executor_route:
        executor_key = (
            _text(executor_route.get("runtime")),
            _text(executor_route.get("provider")),
            _text(executor_route.get("model_id")),
        )
        rejections.setdefault(executor_key, "policy_denied")

    scored: List[Dict[str, Any]] = []
    policy_rejected: List[Dict[str, Any]] = []
    for entry in eligible:
        key = _tiebreak_key(entry)
        reason_code = rejections.get(key)
        if reason_code is not None:
            policy_rejected.append({
                "runtime": entry["runtime"], "provider": entry["provider"], "model_id": entry["model_id"],
                "score": _score(entry, normalized["preferred_capabilities"]),
                "reason_code": reason_code,
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
            block_reason = "all eligible candidates were rejected by policy (independent_review/fallback)"

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
        # Fallback/circuit-breaker linkage (populated by route_with_fallback; a
        # plain route() call always carries the same shape so every receipt this
        # module produces is schema-uniform, whether or not it is a fallback hop).
        "previous_route_id": "",
        "fallback_reason_code": "",
        "fallback_decision": None,
        "retry_backoff_seconds": None,
    }
    return receipt


# ---------------------------------------------------------------------------
# Fallback / circuit-breaker semantics
# ---------------------------------------------------------------------------

def classify_failure(reason_code: str) -> str:
    """Classify a failure reason code as ``"transient"`` or ``"permanent"``.

    Transient failures (timeout, rate limit, momentary unavailability) are
    retryable; permanent failures (missing auth, policy, context/budget limits,
    device mismatch, bad output) never are -- retrying the same or another
    candidate would not change the outcome.
    """
    code = _text(reason_code)
    cls = FAILURE_REASON_CLASSES.get(code)
    if cls is None:
        raise ModelRouterError(
            f"unknown failure reason_code {code!r}; expected one of {sorted(FAILURE_REASON_CODES)}"
        )
    return cls


def fallback_decision(reason_code: str, *, attempt: int, max_routes: int) -> str:
    """Decide ``retry_same_route`` / ``fallback_route`` / ``terminal`` for one failure.

    ``attempt`` is the 1-based count of consecutive failures already observed on
    the current route (including the one being classified now). ``max_routes`` is
    the ceiling on total route hops (including the first) the task-contract's
    fallback policy allows.
    """
    if attempt < 1:
        raise ModelRouterError("attempt must be >= 1")
    if max_routes < 1:
        raise ModelRouterError("max_routes must be >= 1")
    failure_class = classify_failure(reason_code)
    if failure_class == "permanent":
        return "terminal"
    if attempt >= max_routes:
        return "terminal"
    if attempt <= 1:
        return "retry_same_route"
    return "fallback_route"


def compute_backoff(attempt: int, *, base_seconds: float = 1.0, cap_seconds: float = 30.0,
                     jitter_seed: str = "") -> float:
    """Deterministic exponential backoff with optional seeded jitter.

    Same ``(attempt, base_seconds, cap_seconds, jitter_seed)`` always yields the
    same delay -- reproducible in tests and receipts, unlike wall-clock/PRNG
    jitter. Jitter (when a non-empty ``jitter_seed`` is given) adds up to 25% of
    the capped delay, derived from a content hash so it varies by attempt/seed
    without depending on process-global random state.
    """
    if attempt < 1:
        raise ModelRouterError("attempt must be >= 1")
    if base_seconds < 0 or cap_seconds < 0:
        raise ModelRouterError("base_seconds/cap_seconds must be >= 0")
    raw = base_seconds * (2 ** (attempt - 1))
    capped = min(raw, cap_seconds)
    jitter = 0.0
    if jitter_seed:
        digest = hashlib.sha256(f"{jitter_seed}:{attempt}".encode("utf-8")).hexdigest()
        frac = int(digest[:8], 16) / 0xFFFFFFFF
        jitter = frac * capped * 0.25
    return round(capped + jitter, 3)


class CircuitState:
    """Circuit-breaker state vocabulary."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


ALL_CIRCUIT_STATES = (CircuitState.CLOSED, CircuitState.OPEN, CircuitState.HALF_OPEN)


class CircuitBreaker:
    """Per-(runtime, provider, model_id) failure tracker.

    Deterministic and in-memory: every method accepts an explicit ``now`` so
    tests (and any durable wrapper a caller wants to add) never depend on
    wall-clock timing. A route's circuit opens after ``failure_threshold``
    consecutive failures and stays open for ``cooldown_seconds`` before moving
    to ``half_open`` (one probe allowed through); a subsequent success closes it
    again, a subsequent failure re-opens it.
    """

    def __init__(self, *, failure_threshold: int = 3, cooldown_seconds: float = 30.0) -> None:
        if failure_threshold < 1:
            raise ModelRouterError("failure_threshold must be >= 1")
        if cooldown_seconds < 0:
            raise ModelRouterError("cooldown_seconds must be >= 0")
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._counts: Dict[Tuple[str, str, str], int] = {}
        self._opened_at: Dict[Tuple[str, str, str], float] = {}
        self._half_open: Set[Tuple[str, str, str]] = set()

    def record_failure(self, key: Tuple[str, str, str], *, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        count = self._counts.get(key, 0) + 1
        self._counts[key] = count
        if count >= self.failure_threshold:
            self._opened_at[key] = now
            self._half_open.discard(key)

    def record_success(self, key: Tuple[str, str, str]) -> None:
        self._counts.pop(key, None)
        self._opened_at.pop(key, None)
        self._half_open.discard(key)

    def state(self, key: Tuple[str, str, str], *, now: Optional[float] = None) -> str:
        now = time.time() if now is None else now
        opened_at = self._opened_at.get(key)
        if opened_at is None:
            return CircuitState.CLOSED
        if key in self._half_open:
            return CircuitState.HALF_OPEN
        if now - opened_at >= self.cooldown_seconds:
            self._half_open.add(key)
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    def is_available(self, key: Tuple[str, str, str], *, now: Optional[float] = None) -> bool:
        return self.state(key, now=now) != CircuitState.OPEN


def _candidate_rejection_reason(failure_reason_code: str) -> str:
    """Map a failure reason code onto the routing-decision-receipt reason-code
    vocabulary (``model_registry.REASON_CODES``) so a fallback-excluded
    candidate's ``reason_code`` still validates against the receipt schema."""
    code = _text(failure_reason_code)
    if code in REASON_CODES:
        return code
    return "runtime_unavailable"


def route_with_fallback(requirements: Mapping[str, Any], registry: ModelCapabilityRegistry, *,
                         previous_route: Optional[Mapping[str, Any]] = None,
                         previous_route_id: str = "",
                         failure_reason_code: str = "",
                         attempt: int = 1,
                         max_routes: int = 3,
                         circuit_breaker: Optional[CircuitBreaker] = None,
                         executor_route: Optional[Mapping[str, Any]] = None,
                         policy_version: str = POLICY_VERSION) -> Dict[str, Any]:
    """Resolve a routing decision that may be reacting to a prior route's failure.

    With no ``previous_route``/``failure_reason_code`` this behaves exactly like
    :func:`route`. When a prior route failed:

    1. the failure is classified and turned into a fallback decision
       (``retry_same_route`` / ``fallback_route`` / ``terminal``) via
       :func:`fallback_decision`;
    2. on ``terminal``, the receipt is ``blocked=True`` with no new candidate
       search -- retrying would not change the outcome;
    3. on ``fallback_route``, the failed candidate is excluded from this route's
       eligible set (rejected with a schema-valid reason code) and, if a
       ``circuit_breaker`` was supplied, its failure is recorded there too so
       future calls see it as unavailable once the threshold trips;
    4. on ``retry_same_route``, the failed candidate stays eligible so ``route()``
       is free to reselect it;
    5. every circuit-open candidate (per ``circuit_breaker``) is excluded from
       this route regardless of which branch above applies.

    The returned receipt always carries ``previous_route_id``,
    ``fallback_reason_code``, ``fallback_decision`` and a deterministic
    ``retry_backoff_seconds`` (via :func:`compute_backoff`), linking this
    decision back to the one it supersedes -- never a silent runtime swap.
    """
    if attempt < 1:
        raise ModelRouterError("attempt must be >= 1")
    if max_routes < 1:
        raise ModelRouterError("max_routes must be >= 1")

    has_failure = bool(previous_route) and bool(_text(failure_reason_code))
    policy_rejections: Dict[Tuple[str, str, str], str] = {}
    decision: Optional[str] = None
    backoff_seconds: Optional[float] = None
    prev_key: Optional[Tuple[str, str, str]] = None

    if has_failure:
        prev_key = (
            _text(previous_route.get("runtime")),
            _text(previous_route.get("provider")),
            _text(previous_route.get("model_id")),
        )
        if circuit_breaker is not None:
            circuit_breaker.record_failure(prev_key)
        decision = fallback_decision(failure_reason_code, attempt=attempt, max_routes=max_routes)
        if decision == "terminal":
            normalized = _normalize_requirements(requirements)
            return {
                "schema": SCHEMA,
                "policy_version": _text(policy_version) or POLICY_VERSION,
                "registry_hash": registry.registry_hash,
                "requirements": normalized,
                "candidates": [],
                "selected": None,
                "blocked": True,
                "block_reason": f"terminal failure on previous route: {_text(failure_reason_code)}",
                "timestamp": _now(),
                "previous_route_id": _text(previous_route_id),
                "fallback_reason_code": _text(failure_reason_code),
                "fallback_decision": decision,
                "retry_backoff_seconds": None,
            }
        backoff_seconds = compute_backoff(attempt, jitter_seed=_text(previous_route_id))
        if decision == "fallback_route":
            policy_rejections[prev_key] = _candidate_rejection_reason(failure_reason_code)

    if circuit_breaker is not None:
        for entry in registry.entries:
            key = _tiebreak_key(entry)
            if key not in policy_rejections and not circuit_breaker.is_available(key):
                policy_rejections[key] = "runtime_unavailable"

    receipt = route(
        requirements, registry,
        executor_route=executor_route,
        policy_version=policy_version,
        policy_rejections=policy_rejections,
    )
    receipt["previous_route_id"] = _text(previous_route_id)
    receipt["fallback_reason_code"] = _text(failure_reason_code)
    receipt["fallback_decision"] = decision
    receipt["retry_backoff_seconds"] = backoff_seconds
    return receipt


__all__ = [
    "ALL_CIRCUIT_STATES",
    "CircuitBreaker",
    "CircuitState",
    "FAILURE_REASON_CLASSES",
    "FAILURE_REASON_CODES",
    "FALLBACK_DECISIONS",
    "ModelRouterError",
    "POLICY_VERSION",
    "ROLES",
    "SCHEMA",
    "classify_failure",
    "compute_backoff",
    "fallback_decision",
    "route",
    "route_with_fallback",
]
