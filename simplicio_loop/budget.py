"""Shared run budget and delta/context-pack primitives.

Budget state is a MapperStore operations-journal projection.  Loop retains the
policy and pure contract types, while Mapper owns the durable SQLite authority,
hash chain, and cross-process compare-and-swap boundary.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Union

from .mapper_operations import MapperOperationsAdapter

RUN_BUDGET_SCHEMA = "simplicio.run-budget/v1"
RESERVATION_SCHEMA = "simplicio.budget-reservation/v1"
SETTLEMENT_SCHEMA = "simplicio.usage-settlement/v1"
CONTEXT_PACK_SCHEMA = "simplicio.context-pack-ref/v1"
DELTA_SCHEMA = "simplicio.continuation-delta/v1"
BUDGET_EVENT_SCHEMA = "simplicio.loop-budget-event/v1"
BUDGET_JOURNAL_PREFIX = "simplicio.loop.budget:"


class BudgetError(RuntimeError):
    """Base error for fail-closed budget operations."""


class BudgetExceeded(BudgetError):
    """The configured envelope cannot admit or settle the requested usage."""


class UnknownReservation(BudgetError):
    """A settlement/cancellation referred to no known reservation."""


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _now() -> float:
    return time.time()


@dataclass(frozen=True)
class RunBudget:
    run_id: str
    token_limit: int
    call_limit: int = 0
    cost_limit_micros: int = 0
    latency_limit_ms: int = 0
    exhaustion_policy: str = "stop"

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        for name in ("token_limit", "call_limit", "cost_limit_micros", "latency_limit_ms"):
            if getattr(self, name) < 0:
                raise ValueError(name + " must be non-negative")
        if self.exhaustion_policy not in ("stop", "compress", "serial", "downgrade", "escalate"):
            raise ValueError("unsupported exhaustion policy")

    def as_dict(self) -> Dict[str, Any]:
        return {"schema": RUN_BUDGET_SCHEMA, "run_id": self.run_id,
                "token_limit": self.token_limit, "call_limit": self.call_limit,
                "cost_limit_micros": self.cost_limit_micros,
                "latency_limit_ms": self.latency_limit_ms,
                "exhaustion_policy": self.exhaustion_policy}


class BudgetLedger:
    """Durable, cross-process run budget ledger projected from MapperStore events."""

    def __init__(self, path: Union[str, Path], budget: RunBudget, *, operations: Any | None = None):
        self.path = str(path)
        self.budget = budget
        self._lock = threading.RLock()
        self._journal_id = BUDGET_JOURNAL_PREFIX + budget.run_id
        self._operations = operations or MapperOperationsAdapter(self.path)
        self._operations.initialize()
        self._ensure_envelope()

    def _replay(self) -> dict[str, Any]:
        replay = self._operations.replay(self._journal_id)
        if not replay.get("valid", False):
            raise BudgetError("budget journal is invalid")
        return replay

    @staticmethod
    def _last_seq(replay: Mapping[str, Any]) -> int:
        events = replay.get("events") or []
        if events:
            return int(events[-1]["seq"])
        compaction = replay.get("compaction")
        return int(compaction["through_seq"]) if compaction else 0

    def _state(self, replay: Mapping[str, Any] | None = None) -> dict[str, Any]:
        state: dict[str, Any] = {
            "envelope": None,
            "reservations": {},
            "settlements": {},
            "spent_tokens": 0,
            "spent_calls": 0,
            "spent_cost_micros": 0,
            "spent_latency_ms": 0,
        }
        for event in (replay or self._replay()).get("events", []):
            event_type = event.get("event_type")
            payload = event.get("payload")
            if not isinstance(payload, Mapping) or payload.get("schema") != BUDGET_EVENT_SCHEMA:
                raise BudgetError("budget journal contains an unknown event")
            if event_type == "budget.initialized":
                envelope = dict(payload["envelope"])
                if state["envelope"] is not None and state["envelope"] != envelope:
                    raise BudgetError("run budget is immutable after freeze")
                state["envelope"] = envelope
            elif event_type == "budget.reserved":
                reservation = dict(payload["reservation"])
                reservation_id = reservation["reservation_id"]
                prior = state["reservations"].get(reservation_id)
                if prior is not None and prior != reservation:
                    raise BudgetError("reservation journal conflict")
                state["reservations"][reservation_id] = reservation
            elif event_type == "budget.settled":
                settlement = dict(payload["settlement"])
                reservation_id = settlement["reservation_id"]
                prior = state["settlements"].get(reservation_id)
                if prior is not None:
                    if prior != settlement:
                        raise BudgetError("settlement journal conflict")
                    continue
                reservation = state["reservations"].get(reservation_id)
                if reservation is None:
                    raise BudgetError("settlement references unknown reservation")
                reservation["state"] = "settled"
                state["spent_tokens"] += int(settlement["tokens"])
                state["spent_calls"] += int(settlement["calls"])
                state["spent_cost_micros"] += int(settlement["cost_micros"])
                state["spent_latency_ms"] += int(settlement["latency_ms"])
                state["settlements"][reservation_id] = settlement
            elif event_type == "budget.cancelled":
                reservation_id = str(payload["reservation_id"])
                reservation = state["reservations"].get(reservation_id)
                if reservation is None:
                    raise BudgetError("cancellation references unknown reservation")
                reservation["state"] = "cancelled"
        if state["envelope"] is None:
            raise BudgetError("run budget envelope is not initialized")
        return state

    @staticmethod
    def _is_conflict(error: BaseException) -> bool:
        return getattr(error, "reason_code", "") == "JOURNAL_CONFLICT" or "JOURNAL_CONFLICT" in str(error)

    def _append(self, event_type: str, payload: Mapping[str, Any], *, expected_seq: int) -> None:
        try:
            self._operations.append_event(
                self._journal_id,
                event_type,
                {"schema": BUDGET_EVENT_SCHEMA, **dict(payload)},
                expected_seq=expected_seq,
            )
        except Exception as error:
            if self._is_conflict(error):
                raise BudgetError("JOURNAL_CONFLICT") from error
            raise BudgetError("budget journal append failed: " + str(error)) from error

    def _ensure_envelope(self) -> None:
        for _ in range(32):
            replay = self._replay()
            try:
                state = self._state(replay)
            except BudgetError as error:
                if "not initialized" not in str(error):
                    raise
                state = None
            if state is not None:
                if state["envelope"] != self.budget.as_dict():
                    raise BudgetError("run budget is immutable after freeze")
                return
            try:
                self._append(
                    "budget.initialized",
                    {"envelope": self.budget.as_dict()},
                    expected_seq=self._last_seq(replay),
                )
                return
            except BudgetError as error:
                if "JOURNAL_CONFLICT" not in str(error):
                    raise
        raise BudgetError("budget envelope remained contended")

    def _totals(self, state: Mapping[str, Any]) -> tuple[int, int, int, int, int, int, int, int]:
        reserved = [value for value in state["reservations"].values() if value["state"] == "reserved"]
        return (
            int(state["spent_tokens"]), int(state["spent_calls"]),
            int(state["spent_cost_micros"]), int(state["spent_latency_ms"]),
            sum(int(value["estimate_tokens"]) for value in reserved),
            sum(int(value["estimate_calls"]) for value in reserved),
            sum(int(value["estimate_cost"]) for value in reserved),
            sum(int(value["estimate_latency"]) for value in reserved),
        )

    def _exceeds(self, totals: tuple[int, int, int, int, int, int, int, int], *, tokens: int,
                 calls: int, cost_micros: int, latency_ms: int) -> bool:
        spent_t, spent_c, spent_cost, spent_lat, reserved_t, reserved_c, reserved_cost, reserved_lat = totals
        return (
            spent_t + reserved_t + tokens > self.budget.token_limit
            or (self.budget.call_limit and spent_c + reserved_c + calls > self.budget.call_limit)
            or (self.budget.cost_limit_micros and spent_cost + reserved_cost + cost_micros > self.budget.cost_limit_micros)
            or (self.budget.latency_limit_ms and spent_lat + reserved_lat + latency_ms > self.budget.latency_limit_ms)
        )

    def reserve(self, reservation_id: str, work_item_id: str, *, tokens: int, calls: int = 1,
                cost_micros: int = 0, latency_ms: int = 0, expires_at: Optional[float] = None) -> Dict[str, Any]:
        values = (tokens, calls, cost_micros, latency_ms)
        if not reservation_id.strip() or not work_item_id.strip() or any(v < 0 for v in values):
            raise ValueError("reservation ids and estimates must be valid")
        with self._lock:
            for _ in range(32):
                replay = self._replay()
                state = self._state(replay)
                prior = state["reservations"].get(reservation_id)
                if prior is not None:
                    if (prior["run_id"], prior["work_item_id"], prior["estimate_tokens"], prior["estimate_calls"], prior["estimate_cost"], prior["estimate_latency"]) != (self.budget.run_id, work_item_id, tokens, calls, cost_micros, latency_ms):
                        raise BudgetError("reservation id reused with different estimate")
                    return dict(prior)
                if self._exceeds(self._totals(state), tokens=tokens, calls=calls, cost_micros=cost_micros, latency_ms=latency_ms):
                    raise BudgetExceeded("shared run budget exhausted")
                reservation = {"schema": RESERVATION_SCHEMA, "reservation_id": reservation_id,
                               "run_id": self.budget.run_id, "work_item_id": work_item_id,
                               "tokens": tokens, "calls": calls, "cost_micros": cost_micros,
                               "latency_ms": latency_ms, "estimate_tokens": tokens,
                               "estimate_calls": calls, "estimate_cost": cost_micros,
                               "estimate_latency": latency_ms, "state": "reserved",
                               "created_at": _now(), "expires_at": expires_at}
                try:
                    self._append(
                        "budget.reserved",
                        {"reservation": reservation},
                        expected_seq=self._last_seq(replay),
                    )
                    return reservation
                except BudgetError as error:
                    if "JOURNAL_CONFLICT" not in str(error):
                        raise
            raise BudgetError("budget reservation remained contended")

    def settle(self, reservation_id: str, *, tokens: int, calls: int = 1, cost_micros: int = 0,
               latency_ms: int = 0, status: str = "completed") -> Dict[str, Any]:
        if any(v < 0 for v in (tokens, calls, cost_micros, latency_ms)):
            raise ValueError("usage must be non-negative")
        with self._lock:
            for _ in range(32):
                replay = self._replay()
                state = self._state(replay)
                prior = state["settlements"].get(reservation_id)
                if prior is not None:
                    return dict(prior)
                reservation = state["reservations"].get(reservation_id)
                if reservation is None:
                    raise UnknownReservation(reservation_id)
                if reservation["state"] != "reserved":
                    raise BudgetError("reservation is not settleable")
                totals = list(self._totals(state))
                totals[4] -= int(reservation["estimate_tokens"])
                totals[5] -= int(reservation["estimate_calls"])
                totals[6] -= int(reservation["estimate_cost"])
                totals[7] -= int(reservation["estimate_latency"])
                if self._exceeds(tuple(totals), tokens=tokens, calls=calls, cost_micros=cost_micros, latency_ms=latency_ms):
                    raise BudgetExceeded("late usage receipt would overspend shared run budget")
                settlement = {"schema": SETTLEMENT_SCHEMA, "reservation_id": reservation_id,
                              "run_id": self.budget.run_id, "work_item_id": reservation["work_item_id"],
                              "tokens": tokens, "calls": calls, "cost_micros": cost_micros,
                              "latency_ms": latency_ms, "status": status}
                try:
                    self._append(
                        "budget.settled",
                        {"settlement": settlement},
                        expected_seq=self._last_seq(replay),
                    )
                    return settlement
                except BudgetError as error:
                    if "JOURNAL_CONFLICT" not in str(error):
                        raise
            raise BudgetError("budget settlement remained contended")

    def cancel(self, reservation_id: str) -> bool:
        with self._lock:
            for _ in range(32):
                replay = self._replay()
                state = self._state(replay)
                reservation = state["reservations"].get(reservation_id)
                if reservation is None:
                    raise UnknownReservation(reservation_id)
                if reservation["state"] != "reserved":
                    return False
                try:
                    self._append(
                        "budget.cancelled",
                        {"reservation_id": reservation_id},
                        expected_seq=self._last_seq(replay),
                    )
                    return True
                except BudgetError as error:
                    if "JOURNAL_CONFLICT" not in str(error):
                        raise
            raise BudgetError("budget cancellation remained contended")

    def snapshot(self) -> Dict[str, Any]:
        state = self._state()
        spent_t, spent_c, spent_cost, spent_lat, reserved_t, reserved_c, reserved_cost, reserved_lat = self._totals(state)
        return {"schema": RUN_BUDGET_SCHEMA, "run_id": self.budget.run_id,
                "limits": self.budget.as_dict(), "spent_tokens": spent_t, "spent_calls": spent_c,
                "spent_cost_micros": spent_cost, "spent_latency_ms": spent_lat,
                "reserved_tokens": reserved_t, "reserved_calls": reserved_c,
                "reserved_cost_micros": reserved_cost, "reserved_latency_ms": reserved_lat,
                "remaining_tokens": max(0, self.budget.token_limit - spent_t - reserved_t),
                "exhaustion_policy": self.budget.exhaustion_policy}


@dataclass(frozen=True)
class ContextPackRef:
    pack_hash: str
    goal_hash: str
    relevant_fingerprint: str
    revision: int = 1

    def as_dict(self) -> Dict[str, Any]:
        return {"schema": CONTEXT_PACK_SCHEMA, "pack_hash": self.pack_hash,
                "goal_hash": self.goal_hash, "relevant_fingerprint": self.relevant_fingerprint,
                "revision": self.revision}


def context_pack_ref(*, goal: str, policy: Mapping[str, Any], acceptance: Sequence[str],
                     relevant_fingerprint: str, revision: int = 1) -> ContextPackRef:
    stable = {"goal": goal, "policy": dict(policy), "acceptance": list(acceptance)}
    pack_hash = hashlib.sha256(_json(stable).encode("utf-8")).hexdigest()
    goal_hash = hashlib.sha256(goal.encode("utf-8")).hexdigest()
    return ContextPackRef(pack_hash, goal_hash, relevant_fingerprint, revision)


def continuation_delta(events: Iterable[Mapping[str, Any]], acknowledged_cursor: int = 0,
                       *, pack: ContextPackRef, force_full: bool = False) -> Dict[str, Any]:
    """Return only sequenced events after the cursor; full history is explicit."""
    rows = [dict(event) for event in events]
    if acknowledged_cursor < 0:
        raise ValueError("acknowledged_cursor must be non-negative")
    if any(not isinstance(row.get("seq"), int) or row["seq"] < 1 for row in rows):
        raise BudgetError("continuation events require positive integer seq")
    rows.sort(key=lambda row: row["seq"])
    delta = rows if force_full else [row for row in rows if row["seq"] > acknowledged_cursor]
    return {"schema": DELTA_SCHEMA, "cursor": acknowledged_cursor,
            "next_cursor": max([acknowledged_cursor] + [row["seq"] for row in delta]),
            "pack": pack.as_dict(), "full_history": bool(force_full), "events": delta}


__all__ = ["BudgetError", "BudgetExceeded", "UnknownReservation", "RunBudget", "BudgetLedger",
           "ContextPackRef", "context_pack_ref", "continuation_delta", "RUN_BUDGET_SCHEMA",
           "RESERVATION_SCHEMA", "SETTLEMENT_SCHEMA", "CONTEXT_PACK_SCHEMA", "DELTA_SCHEMA",
           "BUDGET_EVENT_SCHEMA"]
