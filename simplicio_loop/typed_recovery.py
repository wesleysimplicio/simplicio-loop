"""Typed, model-free retry and effect reconciliation.

Known technical failures are handled deterministically.  Semantic failures are
returned as an explicit replan request; this module never starts an LLM.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional


SCHEMA = "simplicio.typed-recovery/v1"
RECEIPT_SCHEMA = "simplicio.recovery-receipt/v1"


class FailureClass(str, Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    POLICY = "policy"
    SEMANTIC = "semantic"
    EFFECT_UNKNOWN = "effect_unknown"


class RecoveryFailure(RuntimeError):
    def __init__(self, failure_class: FailureClass, code: str, detail: str = "") -> None:
        self.failure_class = failure_class
        self.code = code
        self.detail = detail
        super().__init__(detail or code)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    max_elapsed_seconds: float = 30.0
    base_backoff_seconds: float = 0.25
    max_backoff_seconds: float = 5.0
    jitter_ratio: float = 0.1

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.max_elapsed_seconds <= 0:
            raise ValueError("retry budget must be positive")
        if self.base_backoff_seconds < 0 or self.max_backoff_seconds < 0:
            raise ValueError("backoff cannot be negative")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between zero and one")


@dataclass(frozen=True)
class AttemptContext:
    task_id: str
    idempotency_key: str
    attempt_id: str
    parent_attempt_id: str
    attempt: int
    fence: int


def classify_failure(exc: BaseException) -> FailureClass:
    if isinstance(exc, RecoveryFailure):
        return exc.failure_class
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return FailureClass.TRANSIENT
    if isinstance(exc, (PermissionError, FileNotFoundError, ValueError, TypeError)):
        return FailureClass.PERMANENT
    return FailureClass.EFFECT_UNKNOWN


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


class RecoveryJournal:
    """Crash-readable hash-chain and materialized completion index."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.rows = self._read()

    def _read(self) -> list[Dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[Dict[str, Any]] = []
        previous = ""
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line:
                continue
            row = json.loads(line)
            body = dict(row)
            digest = body.pop("receipt_hash", None)
            if (
                row.get("schema") != RECEIPT_SCHEMA
                or row.get("sequence") != len(rows) + 1
                or row.get("previous_hash", "") != previous
                or digest != _hash(body)
            ):
                raise ValueError("invalid recovery journal at line %d" % number)
            previous = str(digest)
            rows.append(row)
        return rows

    def append(self, event: str, **payload: Any) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "schema": RECEIPT_SCHEMA,
            "sequence": len(self.rows) + 1,
            "previous_hash": self.rows[-1]["receipt_hash"] if self.rows else "",
            "event": event,
            "observed_at_ns": time.time_ns(),
            **payload,
        }
        row["receipt_hash"] = _hash(row)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(row, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.rows.append(row)
        return dict(row)

    def completed(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        for row in reversed(self.rows):
            if row.get("idempotency_key") == idempotency_key and row["event"] in {
                "succeeded", "reconciled"
            }:
                return dict(row)
        return None

    def max_fence(self, task_id: str) -> int:
        return max(
            (int(row.get("fence", 0)) for row in self.rows if row.get("task_id") == task_id),
            default=0,
        )


Operation = Callable[[AttemptContext], Awaitable[Any]]
Reconciler = Callable[[str], Awaitable[Mapping[str, Any]]]


class TypedRecoveryController:
    def __init__(
        self,
        journal_path: str,
        *,
        policy: RetryPolicy = RetryPolicy(),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.journal = RecoveryJournal(journal_path)
        self.policy = policy
        self.sleep = sleep
        self.clock = clock

    def _delay(self, task_id: str, attempt: int) -> float:
        raw = min(
            self.policy.max_backoff_seconds,
            self.policy.base_backoff_seconds * (2 ** max(0, attempt - 1)),
        )
        if not raw or not self.policy.jitter_ratio:
            return raw
        digest = hashlib.sha256(("%s:%d" % (task_id, attempt)).encode()).digest()
        unit = int.from_bytes(digest[:8], "big") / float((1 << 64) - 1)
        return raw * (1 - self.policy.jitter_ratio + 2 * self.policy.jitter_ratio * unit)

    async def run(
        self,
        *,
        task_id: str,
        idempotency_key: str,
        operation: Operation,
        reconcile: Optional[Reconciler] = None,
    ) -> Dict[str, Any]:
        prior = self.journal.completed(idempotency_key)
        if prior is not None:
            return {
                "schema": SCHEMA,
                "status": "succeeded",
                "source": "idempotency_receipt",
                "attempts": 0,
                "receipt": prior,
                "llm_invoked": False,
            }

        started = self.clock()
        parent = ""
        fence = self.journal.max_fence(task_id)
        executed = 0
        for attempt in range(1, self.policy.max_attempts + 1):
            remaining = self.policy.max_elapsed_seconds - (self.clock() - started)
            if remaining <= 0:
                break
            fence += 1
            attempt_id = "%s-attempt-%d-fence-%d" % (task_id, attempt, fence)
            context = AttemptContext(
                task_id, idempotency_key, attempt_id, parent, attempt, fence
            )
            executed = attempt
            self.journal.append(
                "attempt_started",
                task_id=task_id,
                idempotency_key=idempotency_key,
                attempt_id=attempt_id,
                parent_attempt_id=parent,
                attempt=attempt,
                fence=fence,
            )
            parent = attempt_id
            try:
                result = await asyncio.wait_for(operation(context), timeout=remaining)
            except asyncio.CancelledError:
                receipt = self.journal.append(
                    "cancelled",
                    task_id=task_id,
                    idempotency_key=idempotency_key,
                    attempt_id=attempt_id,
                    attempt=attempt,
                    fence=fence,
                )
                raise
            except Exception as exc:
                failure_class = classify_failure(exc)
                code = getattr(exc, "code", type(exc).__name__)
                failure = self.journal.append(
                    "attempt_failed",
                    task_id=task_id,
                    idempotency_key=idempotency_key,
                    attempt_id=attempt_id,
                    attempt=attempt,
                    fence=fence,
                    failure_class=failure_class.value,
                    code=str(code),
                )
                if failure_class is FailureClass.EFFECT_UNKNOWN:
                    if reconcile is not None:
                        outcome = dict(await reconcile(idempotency_key))
                        if outcome.get("committed") is True:
                            receipt = self.journal.append(
                                "reconciled",
                                task_id=task_id,
                                idempotency_key=idempotency_key,
                                attempt_id=attempt_id,
                                attempt=attempt,
                                fence=fence,
                                effect_receipt=dict(outcome),
                            )
                            return {
                                "schema": SCHEMA, "status": "succeeded",
                                "source": "reconciliation", "attempts": attempt,
                                "receipt": receipt, "llm_invoked": False,
                            }
                    return {
                        "schema": SCHEMA, "status": "blocked",
                        "reason_code": "effect_unknown", "attempts": attempt,
                        "receipt": failure, "requires_reconciliation": True,
                        "llm_invoked": False,
                    }
                if failure_class is FailureClass.SEMANTIC:
                    return {
                        "schema": SCHEMA, "status": "replan_required",
                        "reason_code": "semantic", "attempts": attempt,
                        "receipt": failure, "requires_semantic_reasoning": True,
                        "llm_invoked": False,
                    }
                if failure_class in {FailureClass.PERMANENT, FailureClass.POLICY}:
                    return {
                        "schema": SCHEMA, "status": "failed",
                        "reason_code": failure_class.value, "attempts": attempt,
                        "receipt": failure, "llm_invoked": False,
                    }
                if attempt >= self.policy.max_attempts:
                    break
                delay = min(self._delay(task_id, attempt), max(0.0, remaining))
                if delay:
                    await self.sleep(delay)
                continue
            receipt = self.journal.append(
                "succeeded",
                task_id=task_id,
                idempotency_key=idempotency_key,
                attempt_id=attempt_id,
                attempt=attempt,
                fence=fence,
                result=result,
            )
            return {
                "schema": SCHEMA, "status": "succeeded", "source": "execution",
                "attempts": attempt, "result": result, "receipt": receipt,
                "llm_invoked": False,
            }

        receipt = self.journal.append(
            "budget_exhausted",
            task_id=task_id,
            idempotency_key=idempotency_key,
            attempts=executed,
            elapsed_seconds=max(0.0, self.clock() - started),
        )
        return {
            "schema": SCHEMA, "status": "failed", "reason_code": "retry_budget_exhausted",
            "attempts": receipt["attempts"], "receipt": receipt, "llm_invoked": False,
        }


__all__ = [
    "AttemptContext", "FailureClass", "RecoveryFailure", "RecoveryJournal",
    "RetryPolicy", "TypedRecoveryController", "classify_failure",
]
