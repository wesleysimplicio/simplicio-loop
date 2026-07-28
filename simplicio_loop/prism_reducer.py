"""Deterministic causal reduction for Prism task and slot receipts."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .hbp_ledger import canonical_sha256

RESULT_SCHEMA = "simplicio.prism-task-result/v1"
SLOT_RECEIPT_SCHEMA = "simplicio.prism-slot-reducer-receipt/v1"
PRISM_RECEIPT_SCHEMA = "simplicio.prism-reducer-receipt/v1"
TERMINAL_VERDICTS = frozenset({"accepted", "failed", "blocked", "cancelled"})


class PrismReducerError(RuntimeError):
    reason_code = "PRISM_REDUCER_ERROR"


def _sha(value: str, name: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise PrismReducerError(f"{name} must be lowercase SHA-256")
    return result


def _items(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted({str(value).strip() for value in values if str(value).strip()}))


@dataclass(frozen=True)
class ExpectedTask:
    task_id: str
    slot_id: str
    owner_agent: str
    fence: int
    source_generation: str
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("task_id", "slot_id", "owner_agent", "source_generation"):
            if not str(getattr(self, name)).strip():
                raise PrismReducerError(f"{name} is required")
        if self.fence < 1:
            raise PrismReducerError("fence must be positive")
        object.__setattr__(self, "depends_on", _items(self.depends_on))
        if self.task_id in self.depends_on:
            raise PrismReducerError("task cannot depend on itself")


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    slot_id: str
    owner_agent: str
    attempt: int
    fence: int
    source_generation: str
    verdict: str
    receipt_hash: str
    artifact_hashes: tuple[str, ...] = ()
    write_set: tuple[str, ...] = ()
    impact_test_hash: str | None = None
    schema: str = RESULT_SCHEMA

    def __post_init__(self) -> None:
        for name in ("task_id", "slot_id", "owner_agent", "source_generation"):
            if not str(getattr(self, name)).strip():
                raise PrismReducerError(f"{name} is required")
        if self.schema != RESULT_SCHEMA:
            raise PrismReducerError("unknown task result schema")
        if min(self.attempt, self.fence) < 1:
            raise PrismReducerError("attempt and fence must be positive")
        if self.verdict not in TERMINAL_VERDICTS:
            raise PrismReducerError("unsupported task verdict")
        object.__setattr__(
            self, "receipt_hash", _sha(self.receipt_hash, "receipt_hash")
        )
        object.__setattr__(
            self,
            "artifact_hashes",
            tuple(
                sorted({_sha(value, "artifact_hash") for value in self.artifact_hashes})
            ),
        )
        object.__setattr__(self, "write_set", _items(self.write_set))
        if self.impact_test_hash is not None:
            object.__setattr__(
                self,
                "impact_test_hash",
                _sha(self.impact_test_hash, "impact_test_hash"),
            )

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @property
    def result_hash(self) -> str:
        return canonical_sha256(self.to_dict())


class PrismReducer:
    """Commutative accumulation followed by deterministic causal reduction."""

    def __init__(self, expected: Sequence[ExpectedTask]) -> None:
        self.expected = {item.task_id: item for item in expected}
        if len(self.expected) != len(expected):
            raise PrismReducerError("duplicate expected task")
        if not self.expected:
            raise PrismReducerError("expected task set cannot be empty")
        for item in expected:
            unknown = set(item.depends_on) - set(self.expected)
            if unknown:
                raise PrismReducerError("expected dependency is unknown")
        self._assert_acyclic()
        self.results: dict[str, TaskResult] = {}
        self.replay_count = 0

    def _assert_acyclic(self) -> None:
        pending = set(self.expected)
        while pending:
            ready = {
                task_id
                for task_id in pending
                if not (set(self.expected[task_id].depends_on) & pending)
            }
            if not ready:
                raise PrismReducerError("expected task graph contains a cycle")
            pending -= ready

    def submit(self, result: TaskResult) -> str:
        expected = self.expected.get(result.task_id)
        if expected is None:
            raise PrismReducerError("result references unknown task")
        if result.slot_id != expected.slot_id:
            raise PrismReducerError("result crosses slot")
        if result.owner_agent != expected.owner_agent:
            raise PrismReducerError("result owner mismatch")
        if result.fence != expected.fence:
            raise PrismReducerError("result fence is stale")
        if result.source_generation != expected.source_generation:
            raise PrismReducerError("result generation is stale")
        previous = self.results.get(result.task_id)
        if previous is not None:
            if previous.result_hash != result.result_hash:
                raise PrismReducerError(
                    "duplicate task result changed immutable content"
                )
            self.replay_count += 1
            return "IDEMPOTENT_REPLAY"
        self.results[result.task_id] = result
        return "ACCEPTED_FOR_REDUCTION"

    def _topological(self, task_ids: set[str]) -> tuple[str, ...]:
        pending = set(task_ids)
        ordered: list[str] = []
        while pending:
            ready = sorted(
                task_id
                for task_id in pending
                if not (set(self.expected[task_id].depends_on) & pending)
            )
            if not ready:
                raise PrismReducerError("result graph cannot be ordered")
            ordered.extend(ready)
            pending.difference_update(ready)
        return tuple(ordered)

    def _conflicts(self, task_ids: Sequence[str]) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        ordered = sorted(task_ids)
        for index, left_id in enumerate(ordered):
            left = self.results[left_id]
            for right_id in ordered[index + 1 :]:
                right = self.results[right_id]
                overlap = sorted(set(left.write_set) & set(right.write_set))
                if not overlap:
                    continue
                dependent = (
                    left_id in self.expected[right_id].depends_on
                    or right_id in self.expected[left_id].depends_on
                )
                if not dependent:
                    conflicts.append(
                        {
                            "left": left_id,
                            "right": right_id,
                            "write_set": overlap,
                            "reason_code": "MECHANICAL_WRITE_CONFLICT",
                        }
                    )
        return conflicts

    def reduce_slot(self, slot_id: str) -> dict[str, Any]:
        expected_ids = {
            task_id
            for task_id, item in self.expected.items()
            if item.slot_id == slot_id
        }
        if not expected_ids:
            raise PrismReducerError("unknown or empty slot")
        present = expected_ids & set(self.results)
        missing = sorted(expected_ids - present)
        ordered = self._topological(present) if present else ()
        conflicts = self._conflicts(ordered)
        blocked_by_dependency: list[str] = []
        for task_id in ordered:
            if any(
                dependency not in self.results
                or self.results[dependency].verdict != "accepted"
                for dependency in self.expected[task_id].depends_on
            ):
                blocked_by_dependency.append(task_id)
        nonaccepted = sorted(
            task_id
            for task_id in present
            if self.results[task_id].verdict != "accepted"
        )
        tests_missing = sorted(
            task_id
            for task_id in present
            if self.results[task_id].verdict == "accepted"
            and self.results[task_id].impact_test_hash is None
        )
        verdict = "accepted"
        reason_code = "REDUCED"
        if missing:
            verdict, reason_code = "partial", "MISSING_RESULTS"
        elif conflicts:
            verdict, reason_code = "blocked", "COMPOSITION_CONFLICT"
        elif blocked_by_dependency:
            verdict, reason_code = "blocked", "DEPENDENCY_NOT_ACCEPTED"
        elif nonaccepted:
            verdict, reason_code = "blocked", "CHILD_NOT_ACCEPTED"
        elif tests_missing:
            verdict, reason_code = "blocked", "IMPACT_TEST_RECEIPT_MISSING"

        payload = {
            "schema": SLOT_RECEIPT_SCHEMA,
            "slot_id": slot_id,
            "expected_task_ids": sorted(expected_ids),
            "result_refs": [
                {
                    "task_id": task_id,
                    "result_hash": self.results[task_id].result_hash,
                    "receipt_hash": self.results[task_id].receipt_hash,
                }
                for task_id in ordered
            ],
            "missing_task_ids": missing,
            "duplicate_task_ids": [],
            "blocked_by_dependency": sorted(blocked_by_dependency),
            "conflicts": conflicts,
            "tests_missing": tests_missing,
            "verdict": verdict,
            "reason_code": reason_code,
            "zero_loss": len(present) + len(missing) == len(expected_ids),
            "requires_delivery_authorization": verdict == "accepted",
            "completion_promoted": False,
        }
        payload["receipt_hash"] = canonical_sha256(payload)
        return payload

    def reduce_prism(self, slot_ids: Sequence[str]) -> dict[str, Any]:
        unique = tuple(sorted(set(slot_ids)))
        if not unique:
            raise PrismReducerError("at least one slot is required")
        slots = [self.reduce_slot(slot_id) for slot_id in unique]
        verdict = (
            "accepted"
            if all(item["verdict"] == "accepted" for item in slots)
            else "blocked"
        )
        payload = {
            "schema": PRISM_RECEIPT_SCHEMA,
            "slot_receipt_refs": [
                {"slot_id": item["slot_id"], "receipt_hash": item["receipt_hash"]}
                for item in slots
            ],
            "verdict": verdict,
            "completion_promoted": False,
            "completion_oracle_input_ready": verdict == "accepted",
            "replay_count": self.replay_count,
        }
        payload["receipt_hash"] = canonical_sha256(payload)
        return payload


__all__ = [
    "PRISM_RECEIPT_SCHEMA",
    "RESULT_SCHEMA",
    "SLOT_RECEIPT_SCHEMA",
    "ExpectedTask",
    "PrismReducer",
    "PrismReducerError",
    "TaskResult",
]
