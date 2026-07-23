"""Mapper-selected context passed to runtime drivers.

The renderer is deliberately provider-neutral.  Repository-derived material is
kept in an untrusted section so it cannot masquerade as an operator constraint.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Tuple


class ContextBudgetError(ValueError):
    """The complete required context cannot fit the dispatch budget."""


class ContextAuthorizationError(ValueError):
    """The request is not bound to the current mapper/plan or target."""


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*['\"]?([A-Za-z0-9_./+=-]{8,})['\"]?"),
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}"),
)


def _redact(value: Any) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: "[REDACTED_SECRET]", text)
    return text


def _stable_items(values: Iterable[Any]) -> Tuple[str, ...]:
    return tuple(sorted({_redact(value).strip() for value in values if _redact(value).strip()}))


@dataclass(frozen=True)
class RuntimeContextRequest:
    """Immutable provider-neutral request built from a Mapper context pack."""

    goal: str
    acceptance_criteria: Tuple[str, ...] = field(default_factory=tuple)
    source_spans: Tuple[str, ...] = field(default_factory=tuple)
    source_refs: Tuple[str, ...] = field(default_factory=tuple)
    verification_routes: Tuple[str, ...] = field(default_factory=tuple)
    graph_evidence: Tuple[str, ...] = field(default_factory=tuple)
    trusted_constraints: Tuple[str, ...] = field(default_factory=tuple)
    untrusted_evidence: Tuple[str, ...] = field(default_factory=tuple)
    authorized_targets: Tuple[str, ...] = field(default_factory=tuple)
    target: str = ""
    remaining_budget_tokens: int = 0
    mapper_envelope_hash: str = ""
    plan_hash: str = ""

    def __post_init__(self) -> None:
        goal = _redact(self.goal).strip()
        if not goal:
            raise ValueError("goal is required")
        if self.remaining_budget_tokens < 0:
            raise ValueError("remaining_budget_tokens must be >= 0")
        object.__setattr__(self, "goal", goal)
        for name in (
            "acceptance_criteria", "source_spans", "source_refs", "verification_routes",
            "graph_evidence", "trusted_constraints", "untrusted_evidence", "authorized_targets",
        ):
            object.__setattr__(self, name, _stable_items(getattr(self, name)))
        object.__setattr__(self, "target", str(self.target or "").replace("\\", "/").strip())
        object.__setattr__(self, "mapper_envelope_hash", str(self.mapper_envelope_hash or "").strip())
        object.__setattr__(self, "plan_hash", str(self.plan_hash or "").strip())

    def as_dict(self) -> dict:
        return {
            "goal": self.goal,
            "acceptance_criteria": self.acceptance_criteria,
            "source_spans": self.source_spans,
            "source_refs": self.source_refs,
            "verification_routes": self.verification_routes,
            "graph_evidence": self.graph_evidence,
            "trusted_constraints": self.trusted_constraints,
            "untrusted_evidence": self.untrusted_evidence,
            "authorized_targets": self.authorized_targets,
            "target": self.target,
            "remaining_budget_tokens": self.remaining_budget_tokens,
            "mapper_envelope_hash": self.mapper_envelope_hash,
            "plan_hash": self.plan_hash,
        }

    @property
    def request_hash(self) -> str:
        encoded = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def validate_context_request(
    request: RuntimeContextRequest,
    *,
    expected_mapper_envelope_hash: Optional[str] = None,
    expected_plan_hash: Optional[str] = None,
) -> None:
    if not request.mapper_envelope_hash or not request.plan_hash:
        raise ContextAuthorizationError("mapper envelope hash and plan hash are required")
    if expected_mapper_envelope_hash is not None and request.mapper_envelope_hash != expected_mapper_envelope_hash:
        raise ContextAuthorizationError("stale mapper envelope hash")
    if expected_plan_hash is not None and request.plan_hash != expected_plan_hash:
        raise ContextAuthorizationError("stale plan hash")
    if not request.target or not request.authorized_targets:
        raise ContextAuthorizationError("authorized target is required")
    target_parts = tuple(part for part in request.target.split("/") if part)
    if request.target.startswith("/") or ".." in target_parts or request.target not in request.authorized_targets:
        raise ContextAuthorizationError("target is outside the authorized task plan")


def _section(title: str, values: Iterable[str]) -> list:
    rows = ["[" + title + "]"]
    rows.extend("- " + _redact(value) for value in values)
    return rows


def render_runtime_context(
    request: RuntimeContextRequest,
    *,
    token_budget: Optional[int] = None,
    expected_mapper_envelope_hash: Optional[str] = None,
    expected_plan_hash: Optional[str] = None,
) -> str:
    """Render all context deterministically, or fail before provider dispatch."""
    validate_context_request(
        request,
        expected_mapper_envelope_hash=expected_mapper_envelope_hash,
        expected_plan_hash=expected_plan_hash,
    )
    rows = [
        "[SIMPLICIO_RUNTIME_CONTEXT/v1]",
        "[GOAL]",
        "- " + request.goal,
    ]
    rows += _section("ACCEPTANCE_CRITERIA", request.acceptance_criteria)
    rows += _section("VERIFICATION_ROUTES", request.verification_routes)
    rows += _section("AUTHORIZED_TARGETS", (request.target,))
    rows += _section("TRUSTED_OPERATOR_CONSTRAINTS", request.trusted_constraints)
    rows += _section("UNTRUSTED_MAPPER_EVIDENCE", request.source_spans + request.source_refs + request.graph_evidence + request.untrusted_evidence)
    rows += ["[END_UNTRUSTED_MAPPER_EVIDENCE]", "[END_SIMPLICIO_RUNTIME_CONTEXT/v1]"]
    rendered = "\n".join(rows)
    budget = request.remaining_budget_tokens if token_budget is None else token_budget
    estimated_tokens = len(rendered.split())
    if budget <= 0 or estimated_tokens > budget:
        raise ContextBudgetError(
            "required runtime context exceeds token budget "
            f"({estimated_tokens} > {budget}); broader context is required"
        )
    return rendered


__all__ = [
    "ContextAuthorizationError", "ContextBudgetError", "RuntimeContextRequest",
    "render_runtime_context", "validate_context_request",
]
