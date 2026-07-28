from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from simplicio_loop.typed_recovery import (
    FailureClass,
    RecoveryFailure,
    RecoveryJournal,
    RetryPolicy,
    TypedRecoveryController,
    classify_failure,
)


def test_failure_taxonomy_is_explicit_and_conservative() -> None:
    assert classify_failure(asyncio.TimeoutError()) is FailureClass.TRANSIENT
    assert classify_failure(ConnectionError()) is FailureClass.TRANSIENT
    assert classify_failure(PermissionError()) is FailureClass.PERMANENT
    assert classify_failure(
        RecoveryFailure(FailureClass.POLICY, "denied")
    ) is FailureClass.POLICY
    assert classify_failure(
        RecoveryFailure(FailureClass.SEMANTIC, "drift")
    ) is FailureClass.SEMANTIC
    assert classify_failure(RuntimeError("ambiguous")) is FailureClass.EFFECT_UNKNOWN


def test_transient_retry_has_budget_backoff_causality_and_fresh_fences(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict, list, list]:
        attempts = []
        delays = []

        async def operation(context):
            attempts.append(context)
            if context.attempt < 3:
                raise ConnectionError("temporary")
            return {"ok": True}

        async def sleep(delay):
            delays.append(delay)

        controller = TypedRecoveryController(
            str(tmp_path / "retry.jsonl"),
            policy=RetryPolicy(
                max_attempts=3, max_elapsed_seconds=10,
                base_backoff_seconds=0.1, jitter_ratio=0,
            ),
            sleep=sleep,
        )
        return await controller.run(
            task_id="T", idempotency_key="effect:T", operation=operation
        ), attempts, delays

    result, attempts, delays = asyncio.run(scenario())
    assert result["status"] == "succeeded"
    assert result["attempts"] == 3
    assert delays == [0.1, 0.2]
    assert [item.fence for item in attempts] == [1, 2, 3]
    assert attempts[0].parent_attempt_id == ""
    assert attempts[1].parent_attempt_id == attempts[0].attempt_id
    assert attempts[2].parent_attempt_id == attempts[1].attempt_id
    assert result["llm_invoked"] is False


def test_permanent_and_policy_fail_once_without_llm(tmp_path: Path) -> None:
    async def scenario(failure_class):
        calls = 0

        async def operation(_context):
            nonlocal calls
            calls += 1
            raise RecoveryFailure(failure_class, "known")

        result = await TypedRecoveryController(
            str(tmp_path / (failure_class.value + ".jsonl")),
            policy=RetryPolicy(max_attempts=10),
        ).run(task_id=failure_class.value, idempotency_key=failure_class.value, operation=operation)
        return result, calls

    for failure_class in (FailureClass.PERMANENT, FailureClass.POLICY):
        result, calls = asyncio.run(scenario(failure_class))
        assert result["status"] == "failed"
        assert result["reason_code"] == failure_class.value
        assert result["attempts"] == calls == 1
        assert result["llm_invoked"] is False


def test_effect_unknown_reconciles_committed_effect_without_repeating_write(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict, int, int]:
        writes = 0
        reconciliations = 0

        async def ambiguous(_context):
            nonlocal writes
            writes += 1
            raise RuntimeError("transport lost after commit")

        async def reconcile(key):
            nonlocal reconciliations
            reconciliations += 1
            return {"committed": True, "idempotency_key": key, "effect_hash": "a" * 64}

        path = str(tmp_path / "effect.jsonl")
        first = await TypedRecoveryController(path).run(
            task_id="write", idempotency_key="write:1",
            operation=ambiguous, reconcile=reconcile,
        )
        second = await TypedRecoveryController(path).run(
            task_id="write", idempotency_key="write:1",
            operation=ambiguous, reconcile=reconcile,
        )
        assert second["source"] == "idempotency_receipt"
        return first, writes, reconciliations

    result, writes, reconciliations = asyncio.run(scenario())
    assert result["status"] == "succeeded"
    assert result["source"] == "reconciliation"
    assert writes == reconciliations == 1


def test_effect_unknown_without_proof_blocks_and_never_retries(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict, int]:
        calls = 0

        async def ambiguous(_context):
            nonlocal calls
            calls += 1
            raise RuntimeError("unknown")

        async def absent(_key):
            return {"committed": False}

        result = await TypedRecoveryController(
            str(tmp_path / "blocked.jsonl"),
            policy=RetryPolicy(max_attempts=10),
        ).run(
            task_id="blocked", idempotency_key="blocked:1",
            operation=ambiguous, reconcile=absent,
        )
        return result, calls

    result, calls = asyncio.run(scenario())
    assert result["status"] == "blocked"
    assert result["reason_code"] == "effect_unknown"
    assert result["requires_reconciliation"] is True
    assert calls == 1


def test_semantic_failure_requests_replan_but_controller_never_calls_llm(tmp_path: Path) -> None:
    async def operation(_context):
        raise RecoveryFailure(FailureClass.SEMANTIC, "acceptance drift")

    result = asyncio.run(
        TypedRecoveryController(str(tmp_path / "semantic.jsonl")).run(
            task_id="semantic", idempotency_key="semantic:1", operation=operation
        )
    )
    assert result["status"] == "replan_required"
    assert result["requires_semantic_reasoning"] is True
    assert result["llm_invoked"] is False


def test_cancel_during_write_releases_resource_and_persists_terminal(tmp_path: Path) -> None:
    async def scenario() -> tuple[bool, list]:
        held = False
        entered = asyncio.Event()

        async def write(_context):
            nonlocal held
            held = True
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                held = False

        path = str(tmp_path / "cancel.jsonl")
        controller = TypedRecoveryController(path)
        task = asyncio.create_task(
            controller.run(task_id="cancel", idempotency_key="cancel:1", operation=write)
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return held, RecoveryJournal(path).rows

    held, rows = asyncio.run(scenario())
    assert held is False
    assert rows[-1]["event"] == "cancelled"


def test_timeout_respects_wall_budget_and_does_not_start_extra_attempt(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict, int]:
        calls = 0

        async def operation(_context):
            nonlocal calls
            calls += 1
            await asyncio.sleep(1)

        result = await TypedRecoveryController(
            str(tmp_path / "timeout.jsonl"),
            policy=RetryPolicy(
                max_attempts=20, max_elapsed_seconds=0.03,
                base_backoff_seconds=0, jitter_ratio=0,
            ),
        ).run(task_id="timeout", idempotency_key="timeout:1", operation=operation)
        return result, calls

    result, calls = asyncio.run(scenario())
    assert result["status"] == "failed"
    assert result["reason_code"] == "retry_budget_exhausted"
    assert calls == 1


def test_crash_restart_uses_next_fence_and_hash_chain_detects_tamper(tmp_path: Path) -> None:
    path = str(tmp_path / "restart.jsonl")

    async def fail(context):
        raise RecoveryFailure(FailureClass.PERMANENT, "stop")

    first = asyncio.run(
        TypedRecoveryController(path).run(
            task_id="restart", idempotency_key="restart:1", operation=fail
        )
    )
    assert first["receipt"]["fence"] == 1

    async def succeed(context):
        return {"fence": context.fence}

    second = asyncio.run(
        TypedRecoveryController(path).run(
            task_id="restart", idempotency_key="restart:2", operation=succeed
        )
    )
    assert second["result"]["fence"] == 2

    journal_path = Path(path)
    journal_path.write_text(
        journal_path.read_text(encoding="utf-8").replace('"event": "succeeded"', '"event": "failed"', 1),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid recovery journal"):
        RecoveryJournal(path)


def test_acceptance_receipt_is_content_addressed() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "tests" / "fixtures" / "typed_recovery_812_receipt.json"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    declared = receipt.pop("receipt_sha")
    encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(encoded).hexdigest() == declared
    for reference in receipt["criteria"].values():
        source_path, test_name = reference.split("::", 1)
        assert ("def %s(" % test_name) in (root / source_path).read_text(encoding="utf-8")
