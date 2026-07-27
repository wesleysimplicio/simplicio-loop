"""Deterministic, conservative validation selection for Loop phases."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Dict, Iterable, Optional, Tuple

VALIDATION_POLICY_SCHEMA_V1 = "simplicio.validation-policy/v1"
VALIDATION_RECEIPT_SCHEMA_V1 = "simplicio.validation-receipt/v1"
_POLICY_VERSION = "v1"
_TIER_ORDER = {"static": 0, "focused": 1, "impacted": 2, "full": 3}
_MANDATORY_CHANGE_KINDS = {
    "api",
    "auth",
    "billing",
    "migration",
    "persistent-format",
    "security",
    "toolchain",
}
_PHASES = {"orient", "edit", "converge", "pre_promote", "critical"}


@dataclass(frozen=True)
class ValidationCandidate:
    name: str
    tier: str
    estimated_ms: int = 0
    independent: bool = True

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("validation candidate name must not be empty")
        if self.tier not in _TIER_ORDER:
            raise ValueError(f"unsupported validation tier: {self.tier}")
        if self.estimated_ms < 0:
            raise ValueError("estimated_ms must be non-negative")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "tier": self.tier,
            "estimated_ms": self.estimated_ms,
            "independent": self.independent,
        }


@dataclass(frozen=True)
class ValidationInputs:
    phase: str
    change_kind: str = "code"
    critical: bool = False
    map_fresh: bool = True
    impact_known: bool = True
    previous_failures: int = 0
    coverage_ratio: Optional[float] = None
    candidates: Tuple[ValidationCandidate, ...] = ()
    cache_context: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.phase not in _PHASES:
            raise ValueError(f"unsupported validation phase: {self.phase}")
        if self.previous_failures < 0:
            raise ValueError("previous_failures must be non-negative")
        if self.coverage_ratio is not None and not 0 <= self.coverage_ratio <= 1:
            raise ValueError("coverage_ratio must be between 0 and 1")

    @classmethod
    def from_mapping(cls, value: Dict[str, Any]) -> "ValidationInputs":
        raw = value.get("candidates", ())
        candidates = tuple(
            item
            if isinstance(item, ValidationCandidate)
            else ValidationCandidate(**item)
            for item in raw
        )
        return cls(
            phase=str(value.get("phase", "edit")),
            change_kind=str(value.get("change_kind", "code")),
            critical=bool(value.get("critical", False)),
            map_fresh=bool(value.get("map_fresh", True)),
            impact_known=bool(value.get("impact_known", True)),
            previous_failures=int(value.get("previous_failures", 0)),
            coverage_ratio=value.get("coverage_ratio"),
            cache_context=tuple(sorted((str(key), str(item)) for key, item in dict(value.get("cache_context") or {}).items())),
            candidates=candidates,
        )


@dataclass(frozen=True)
class ValidationReceipt:
    schema: str
    policy_version: str
    phase: str
    profile: str
    selected_tests: Tuple[str, ...]
    reason_codes: Tuple[str, ...]
    final_gate_required: bool
    cache_allowed: bool
    cache_key: str
    cache_context: Tuple[Tuple[str, str], ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "policy_version": self.policy_version,
            "phase": self.phase,
            "profile": self.profile,
            "selected_tests": list(self.selected_tests),
            "reason_codes": list(self.reason_codes),
            "final_gate_required": self.final_gate_required,
            "cache_allowed": self.cache_allowed,
            "cache_key": self.cache_key,
            "cache_context": dict(self.cache_context),
        }

    def explain(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


class ValidationPolicy:
    """Select the smallest safe validation set without unsafe downgrades."""

    policy_version = _POLICY_VERSION

    def decide(self, inputs: ValidationInputs) -> ValidationReceipt:
        reasons = []
        mandatory = inputs.phase in {"pre_promote", "critical"}
        expanded = (
            inputs.critical
            or inputs.change_kind in _MANDATORY_CHANGE_KINDS
            or not inputs.map_fresh
            or not inputs.impact_known
        )
        if not inputs.map_fresh:
            reasons.append("MAP_STALE")
        if not inputs.impact_known:
            reasons.append("IMPACT_UNKNOWN")
        if inputs.critical:
            reasons.append("CRITICAL_CHANGE")
        if inputs.change_kind in _MANDATORY_CHANGE_KINDS:
            reasons.append("MANDATORY_CHANGE_KIND")
        if inputs.previous_failures:
            reasons.append("PRIOR_FAILURE")

        if inputs.phase == "orient":
            profile, allowed = "orient", {"static"}
        elif inputs.phase == "edit":
            profile, allowed = "edit", {"static", "focused"}
        elif inputs.phase == "converge":
            profile, allowed = "converge", {"static", "focused", "impacted"}
            if inputs.previous_failures >= 2:
                allowed.add("full")
                reasons.append("REPEATED_FAILURE")
        else:
            profile, allowed = inputs.phase, set(_TIER_ORDER)

        if expanded and inputs.phase not in {"pre_promote", "critical"}:
            profile = "converge"
            allowed = set(_TIER_ORDER)
            reasons.append("CONSERVATIVE_ESCALATION")
        if mandatory:
            reasons.append("FINAL_GATE_REQUIRED")

        ordered = sorted(
            (candidate for candidate in inputs.candidates if candidate.tier in allowed),
            key=lambda candidate: (_TIER_ORDER[candidate.tier], candidate.name),
        )
        if profile in {"orient", "edit", "converge"} and not mandatory:
            ordered = self._bounded(ordered)
        selected = tuple(candidate.name for candidate in ordered)
        cache_context = dict(inputs.cache_context)
        cache_context_ready = not cache_context or all(
            cache_context.get(name)
            for name in ("source_hash", "test_hash", "dependency_hash", "environment_hash", "command_hash")
        )
        cache_allowed = (
            inputs.map_fresh
            and inputs.impact_known
            and not inputs.critical
            and inputs.previous_failures == 0
            and not mandatory
            and cache_context_ready
        )
        if not cache_allowed:
            reasons.append("CACHE_DISABLED")
        if not cache_context_ready:
            reasons.append("CACHE_CONTEXT_INCOMPLETE")
        return ValidationReceipt(
            schema=VALIDATION_RECEIPT_SCHEMA_V1,
            policy_version=_POLICY_VERSION,
            phase=inputs.phase,
            profile=profile,
            selected_tests=selected,
            reason_codes=tuple(sorted(set(reasons))),
            final_gate_required=mandatory or expanded,
            cache_allowed=cache_allowed,
            cache_key=self._cache_key(inputs),
            cache_context=inputs.cache_context,
        )

    @staticmethod
    def _bounded(
        candidates: Iterable[ValidationCandidate],
    ) -> Tuple[ValidationCandidate, ...]:
        result = []
        elapsed = 0
        for candidate in candidates:
            if result and (
                len(result) >= 20 or elapsed + candidate.estimated_ms > 120_000
            ):
                continue
            result.append(candidate)
            elapsed += candidate.estimated_ms
        return tuple(result)

    @staticmethod
    def _cache_key(inputs: ValidationInputs) -> str:
        payload = {
            "policy_version": _POLICY_VERSION,
            "phase": inputs.phase,
            "change_kind": inputs.change_kind,
            "critical": inputs.critical,
            "map_fresh": inputs.map_fresh,
            "impact_known": inputs.impact_known,
            "previous_failures": inputs.previous_failures,
            "coverage_ratio": inputs.coverage_ratio,
            "cache_context": dict(inputs.cache_context),
            "candidates": [
                candidate.as_dict()
                for candidate in sorted(
                    inputs.candidates, key=lambda item: (item.name, item.tier)
                )
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "VALIDATION_POLICY_SCHEMA_V1",
    "VALIDATION_RECEIPT_SCHEMA_V1",
    "ValidationCandidate",
    "ValidationInputs",
    "ValidationPolicy",
    "ValidationReceipt",
]
