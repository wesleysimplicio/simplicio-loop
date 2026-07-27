"""Versioned token budget contract for the Loop delivery path."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

TOKEN_BUDGET_SCHEMA = "simplicio.loop.token-budget/v1"
TOKEN_BUDGET_RECEIPT_SCHEMA = "simplicio.loop.token-budget-receipt/v1"
_EXHAUSTION_POLICIES = {"stop", "compress", "serial", "downgrade", "escalate"}


class TokenBudgetError(ValueError):
    """Raised when a token budget or receipt cannot be trusted."""


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _counter(name: str, value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TokenBudgetError(f"{name} must be a non-negative integer or null")
    return value


@dataclass(frozen=True)
class TokenBudget:
    """Immutable hard budget envelope and observable metric contract."""

    run_id: str
    total_tokens: int
    per_attempt_tokens: int
    total_calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_tokens: int | None = None
    context_bytes: int | None = None
    context_tokens: int | None = None
    retry_delta_bytes: int | None = None
    exhaustion_policy: str = "stop"

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise TokenBudgetError("run_id must not be empty")
        for name in ("total_tokens", "per_attempt_tokens", "total_calls"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TokenBudgetError(f"{name} must be a non-negative integer")
        if self.total_tokens < 1 or self.per_attempt_tokens < 1:
            raise TokenBudgetError("token caps must be positive")
        if self.per_attempt_tokens > self.total_tokens:
            raise TokenBudgetError("per_attempt_tokens cannot exceed total_tokens")
        for name in ("input_tokens", "output_tokens", "reasoning_tokens", "cache_tokens", "context_bytes", "context_tokens", "retry_delta_bytes"):
            _counter(name, getattr(self, name))
        if self.exhaustion_policy not in _EXHAUSTION_POLICIES:
            raise TokenBudgetError("unsupported exhaustion policy")

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "schema": TOKEN_BUDGET_SCHEMA,
            "run_id": self.run_id,
            "caps": {
                "total_tokens": self.total_tokens,
                "per_attempt_tokens": self.per_attempt_tokens,
                "total_calls": self.total_calls,
            },
            "metrics": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "reasoning_tokens": self.reasoning_tokens,
                "cache_tokens": self.cache_tokens,
                "context_bytes": self.context_bytes,
                "context_tokens": self.context_tokens,
                "retry_delta_bytes": self.retry_delta_bytes,
            },
            "exhaustion_policy": self.exhaustion_policy,
        }
        payload["budget_key"] = _digest(payload)
        return payload

    def receipt(
        self,
        *,
        spent_tokens: int = 0,
        spent_calls: int = 0,
        attempts: int = 0,
        status: str = "ACTIVE",
        reason_code: str | None = None,
        local_llm_started: bool = False,
    ) -> dict[str, Any]:
        spent_tokens = _counter("spent_tokens", spent_tokens) or 0
        spent_calls = _counter("spent_calls", spent_calls) or 0
        attempts = _counter("attempts", attempts) or 0
        if spent_tokens > self.total_tokens:
            raise TokenBudgetError("spent_tokens exceeds total_tokens")
        if self.total_calls and spent_calls > self.total_calls:
            raise TokenBudgetError("spent_calls exceeds total_calls")
        if not isinstance(status, str) or not status.strip():
            raise TokenBudgetError("status must not be empty")
        if not isinstance(local_llm_started, bool):
            raise TokenBudgetError("local_llm_started must be boolean")
        budget = self.as_dict()
        return {
            "schema": TOKEN_BUDGET_RECEIPT_SCHEMA,
            "budget": budget,
            "budget_key": budget["budget_key"],
            "spent": {"tokens": spent_tokens, "calls": spent_calls, "attempts": attempts},
            "remaining": {"tokens": self.total_tokens - spent_tokens, "calls": max(0, self.total_calls - spent_calls) if self.total_calls else None},
            "status": status,
            "reason_code": reason_code,
            "local_llm_started": local_llm_started,
        }


def validate_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    if receipt.get("schema") != TOKEN_BUDGET_RECEIPT_SCHEMA:
        raise TokenBudgetError("unexpected token budget receipt schema")
    budget = receipt.get("budget")
    if not isinstance(budget, Mapping) or budget.get("schema") != TOKEN_BUDGET_SCHEMA:
        raise TokenBudgetError("receipt budget is missing or invalid")
    if receipt.get("budget_key") != budget.get("budget_key"):
        raise TokenBudgetError("receipt budget key mismatch")
    if not isinstance(receipt.get("local_llm_started"), bool):
        raise TokenBudgetError("local_llm_started must be boolean")
    return dict(receipt)
