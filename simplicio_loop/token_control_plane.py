"""Token-control-plane: aggregate BPE ledger and reflect into scheduling.

This module is the implementation behind issue #569. It ingests token events
from any executor (dev-cli, sprint, agent, runtime), aggregates them into a
per-``(work_item_id, attempt, provider_request_id)`` ledger that is
idempotent (retries / stream chunks are never double-counted), and exposes a
reflection step that, at the end of an attempt, decides scheduling nudges in a
strict, reversible, feature-flag-gated order:

    cache-first -> reduce-context -> choose-model/lanes -> only-then reduce-concurrency

No counter being unavailable may ever suspend the queue: soft limits degrade
open-loop (best-effort) instead of blocking.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence


@dataclass(frozen=True)
class TokenEvent:
    """A single normalized token-usage record from an executor."""

    source: str  # "dev-cli" | "sprint" | "agent" | "runtime"
    work_item_id: str
    attempt: int
    provider_request_id: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    model: str = ""
    timestamp: float = 0.0
    estimate_error: float = 0.0  # signed: actual - estimated (positive = over)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def key(self) -> str:
        """Idempotency key — unique per provider request, not per stream chunk."""
        return f"{self.work_item_id}#{self.attempt}#{self.provider_request_id}"


@dataclass
class LedgerEntry:
    """Aggregated, idempotent record for one provider request."""

    events: List[TokenEvent] = field(default_factory=list)

    @property
    def prompt_tokens(self) -> int:
        # Only the FIRST event for a request carries the authoritative count.
        return self.events[0].prompt_tokens if self.events else 0

    @property
    def completion_tokens(self) -> int:
        return self.events[0].completion_tokens if self.events else 0

    @property
    def cached_tokens(self) -> int:
        return self.events[0].cached_tokens if self.events else 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def estimate_error(self) -> float:
        return self.events[0].estimate_error if self.events else 0.0

    @property
    def model(self) -> str:
        return self.events[0].model if self.events else ""


@dataclass
class BudgetView:
    """Human/agent-facing budget summary for one work item attempt."""

    budget: int
    predicted: int
    actual: int
    savings: int  # cached_tokens counted as savings vs. uncached cost
    estimate_error: float


class TokenControlPlane:
    """Aggregates token events and reflects them into scheduling decisions."""

    def __init__(self, soft_limits: Optional[Mapping[str, float]] = None) -> None:
        # key -> LedgerEntry  (idempotent by provider_request_id)
        self._ledger: Dict[str, LedgerEntry] = {}
        self._lock = threading.RLock()
        # per (work_item_id, attempt) -> list of keys, for fast roll-up
        self._index: Dict[tuple, List[str]] = {}
        self.soft_limits: Dict[str, float] = dict(soft_limits or {})

    # -- ingestion -------------------------------------------------------
    def ingest(self, event: TokenEvent) -> bool:
        """Ingest one token event. Returns True if it was NEW (not a dup).

        Idempotency: a second event sharing the same ``key`` (same
        work_item_id+attempt+provider_request_id) is treated as a duplicate
        stream chunk / retry and is NOT re-aggregated. Only the first event
        for a key contributes tokens.
        """
        if not event.provider_request_id:
            # Without a request id we cannot dedupe; refuse to corrupt the ledger.
            return False
        with self._lock:
            existing = self._ledger.get(event.key)
            if existing is not None:
                # Duplicate request — never double count. Record but ignore tokens.
                existing.events.append(event)
                return False
            self._ledger[event.key] = LedgerEntry(events=[event])
            idx = (event.work_item_id, event.attempt)
            self._index.setdefault(idx, []).append(event.key)
            return True

    def ingest_many(self, events: Iterable[TokenEvent]) -> int:
        """Ingest many events; return count of NEW (non-duplicate) ones."""
        return sum(1 for e in events if self.ingest(e))

    # -- roll-up ---------------------------------------------------------
    def entry_keys(self, work_item_id: str, attempt: int) -> List[str]:
        with self._lock:
            return list(self._index.get((work_item_id, attempt), []))

    def budget_view(
        self, work_item_id: str, attempt: int, budget: int, predicted: int
    ) -> BudgetView:
        actual = 0
        savings = 0
        est_err = 0.0
        with self._lock:
            for key in self.entry_keys(work_item_id, attempt):
                entry = self._ledger[key]
                actual += entry.total_tokens
                savings += entry.cached_tokens
                est_err += entry.estimate_error
        return BudgetView(
            budget=budget,
            predicted=predicted,
            actual=actual,
            savings=savings,
            estimate_error=est_err,
        )

    # -- reflection ------------------------------------------------------
    def reflect(
        self,
        work_item_id: str,
        attempt: int,
        flags: Optional[Mapping[str, bool]] = None,
    ) -> Dict[str, object]:
        """End-of-attempt reflection.

        Order is fixed and only advances while the corresponding feature flag
        is enabled; every decision is reversible because flags can be flipped
        at runtime. Soft limits are consulted but never block: if a limit is
        missing we proceed open-loop.
        """
        flags = dict(flags or {})
        view = self.budget_view(work_item_id, attempt, budget=0, predicted=0)
        decisions: Dict[str, object] = {}
        order = [
            ("cache_first", self._d_cache_first),
            ("reduce_context", self._d_reduce_context),
            ("model_and_lanes", self._d_model_lanes),
            ("reduce_concurrency", self._d_reduce_concurrency),
        ]
        for name, fn in order:
            if not flags.get(name, True):
                decisions[name] = "skipped_by_flag"
                continue
            decisions[name] = fn(view)
        return decisions

    # Each decision is a pure function of the budget view + soft limits.
    def _d_cache_first(self, view: BudgetView) -> str:
        if view.savings <= 0 and self._limit("cache_hit_rate", 0.0) > 0:
            return "encourage_cache"
        return "cache_ok"

    def _d_reduce_context(self, view: BudgetView) -> str:
        if view.actual > 0 and self._limit("context_cap", 0.0) > 0:
            return "trim_context"
        return "context_ok"

    def _d_model_lanes(self, view: BudgetView) -> str:
        if view.estimate_error > self._limit("estimate_error_warn", 0.0):
            return "downgrade_model_or_lanes"
        return "model_ok"

    def _d_reduce_concurrency(self, view: BudgetView) -> str:
        # LAST resort only. Never blocks the queue if the limit is absent.
        if self._limit("max_concurrency", 0.0) > 0:
            return "consider_lower_concurrency"
        return "concurrency_open"

    def _limit(self, name: str, default: float) -> float:
        """Soft limit lookup. Missing limit => open-loop (default)."""
        return float(self.soft_limits.get(name, default))

    # -- soft limits -----------------------------------------------------
    def apply_soft_limits(self, limits: Mapping[str, float]) -> None:
        """Update soft limits. A missing counter never suspends the queue."""
        with self._lock:
            self.soft_limits.update({k: float(v) for k, v in limits.items()})


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_str(value: object, default: str = "") -> str:
    return default if value is None else str(value)


def aggregate_from_runtimes(
    plane: TokenControlPlane,
    runtime_events: Sequence[Mapping[str, object]],
) -> int:
    """Normalize heterogeneous runtime payloads into TokenEvents and ingest.

    Returns the number of NEW events. Unknown/malformed payloads are skipped
    (never raise) so one bad runtime cannot stall the plane.
    """
    new = 0
    for raw in runtime_events:
        if not isinstance(raw, Mapping):
            continue
        try:
            ev = TokenEvent(
                source=_as_str(raw.get("source"), "runtime"),
                work_item_id=_as_str(raw.get("work_item_id"), ""),
                attempt=_as_int(raw.get("attempt"), 0),
                provider_request_id=_as_str(raw.get("provider_request_id"), ""),
                prompt_tokens=_as_int(raw.get("prompt_tokens"), 0),
                completion_tokens=_as_int(raw.get("completion_tokens"), 0),
                cached_tokens=_as_int(raw.get("cached_tokens"), 0),
                model=_as_str(raw.get("model"), ""),
                timestamp=_as_float(raw.get("timestamp"), 0.0),
                estimate_error=_as_float(raw.get("estimate_error"), 0.0),
            )
        except (TypeError, ValueError):
            continue
        if plane.ingest(ev):
            new += 1
    return new
