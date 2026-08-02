from __future__ import annotations

from pathlib import Path

import pytest

from simplicio_loop.agent_slots import AgentSlotRegistry, AgentSlotValidationError, cli_main


def registry(tmp_path: Path, capacity: int = 6, retry_limit: int = 1) -> AgentSlotRegistry:
    return AgentSlotRegistry(tmp_path / "slots.sqlite", capacity=capacity, retry_limit=retry_limit)


def test_status_exposes_active_terminal_reclaimable_and_capacity(tmp_path: Path) -> None:
    slots = registry(tmp_path)
    for index in range(6):
        assert slots.acquire("agent-%d" % index)["accepted"]
        assert slots.start("agent-%d" % index)["accepted"]
    blocked = slots.acquire("agent-overflow")
    assert blocked["reason_code"] == "slot_capacity_exhausted"
    assert blocked["diagnostics"]["capacity_holders"] == ["agent-%d" % index for index in range(6)]
    assert slots.close_agent("agent-0", status="completed")["accepted"]
    assert slots.close_agent("agent-1", status="shutdown")["accepted"]
    assert slots.reclaim("agent-0")["accepted"]
    snapshot = slots.status()
    assert snapshot["active_slots"] == 4
    assert snapshot["available_slots"] == 2
    assert snapshot["counts"]["completed"] == 1
    assert snapshot["counts"]["shutdown"] == 1
    assert snapshot["counts"]["reclaimable"] == 2


def test_new_batch_of_six_is_admitted_after_terminal_lanes(tmp_path: Path) -> None:
    slots = registry(tmp_path)
    for index in range(6):
        slots.acquire("old-%d" % index)
        slots.start("old-%d" % index)
        slots.close_agent("old-%d" % index, status="completed")
    outcome = slots.spawn_batch(["new-%d" % index for index in range(6)], lambda _id, _record: True)
    assert all(item["success"] for item in outcome["results"])
    assert outcome["status"]["active_slots"] == 6


def test_failed_spawn_retries_once_without_duplicate_agent_creation(tmp_path: Path) -> None:
    slots = registry(tmp_path, retry_limit=1)
    calls = []

    def spawn(agent_id, record):
        calls.append((agent_id, record["attempt"]))
        return len(calls) == 2

    outcome = slots.spawn_batch(["retry-me"], spawn)
    assert outcome["results"][0]["success"] is True
    assert calls == [("retry-me", 1), ("retry-me", 2)]
    assert len(slots.status()["records"]) == 1
    assert slots.status()["records"][0]["status"] == "running"


def test_retry_is_bounded_and_failure_receipt_is_actionable(tmp_path: Path) -> None:
    slots = registry(tmp_path, retry_limit=1)
    outcome = slots.spawn_batch(["always-fails"], lambda _id, _record: False)
    result = outcome["results"][0]
    assert result["success"] is False
    assert result["attempts"] == 2
    assert result["reason_code"] == "spawn_failed_retry_exhausted"
    assert outcome["status"]["active_slots"] == 0
    assert outcome["status"]["counts"]["reclaimable"] == 1


def test_duplicate_active_agent_never_creates_a_second_slot(tmp_path: Path) -> None:
    slots = registry(tmp_path, capacity=2)
    first = slots.acquire("same")
    second = slots.acquire("same")
    assert first["accepted"] is True
    assert second["reason_code"] == "duplicate_agent"
    assert len(slots.status()["records"]) == 1


def test_reclaim_reports_descendant_worktree_and_lease_blockers(tmp_path: Path) -> None:
    slots = registry(tmp_path)
    slots.acquire("blocked")
    slots.start("blocked")
    slots.close_agent("blocked", status="shutdown")
    slots.update_blockers("blocked", descendants=2, worktree_active=True, lease_active=True)
    receipt = slots.reclaim("blocked")
    assert receipt["accepted"] is False
    assert receipt["reason_code"] == "reclaim_blocked"
    assert receipt["diagnostics"]["blocked"] == [{
        "agent_id": "blocked", "status": "shutdown",
        "blockers": ["descendants", "worktree", "lease"],
    }]
    assert slots.status()["available_slots"] == 6


def test_reclaim_is_idempotent_and_persistence_survives_new_registry(tmp_path: Path) -> None:
    slots = registry(tmp_path)
    slots.acquire("persisted")
    slots.close_agent("persisted", status="completed")
    first = slots.reclaim("persisted")
    second = slots.reclaim("persisted")
    assert first["accepted"] is True
    assert second["accepted"] is True
    reopened = registry(tmp_path)
    assert reopened.status()["counts"]["reclaimable"] == 1
    assert reopened.status()["available_slots"] == 6


def test_invalid_configuration_and_status_are_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(AgentSlotValidationError):
        registry(tmp_path, capacity=0)
    with pytest.raises(AgentSlotValidationError):
        AgentSlotRegistry(tmp_path / "slots.sqlite", capacity=6, retry_limit=-1)
    slots = registry(tmp_path)
    assert slots.close_agent("missing", status="completed")["reason_code"] == "unknown_agent"


def test_cli_exposes_the_complete_lifecycle_as_json(tmp_path: Path, capsys) -> None:
    db = tmp_path / "cli.sqlite"
    legacy = ["--route", "legacy", "--db", str(db)]
    assert cli_main(["status", *legacy]) == 0
    assert cli_main(["acquire", "cli-agent", *legacy, "--worktree", "wt", "--lease-id", "lease"]) == 0
    assert cli_main(["start", "cli-agent", *legacy]) == 0
    assert cli_main(["update-blockers", "cli-agent", *legacy, "--descendants", "0"]) == 0
    assert cli_main(["close", "cli-agent", "--status", "shutdown", *legacy]) == 0
    assert cli_main(["reclaim", "cli-agent", *legacy]) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert len(lines) == 6
    assert '"schema": "simplicio.loop-agent-slot-receipt/v1"' not in lines[0]
    assert '"reclaimed": ["cli-agent"]' in lines[-1]


def test_validation_and_persisted_capacity_conflicts_are_fail_closed(tmp_path: Path) -> None:
    slots = registry(tmp_path)
    with pytest.raises(AgentSlotValidationError):
        slots.acquire(" ")
    with pytest.raises(AgentSlotValidationError):
        slots.update_blockers("missing", descendants=-1)
    with pytest.raises(AgentSlotValidationError):
        slots.update_blockers("missing", worktree_active=1)
    with pytest.raises(AgentSlotValidationError):
        slots.close_agent("missing", status="running")
    slots.acquire("terminal")
    slots.close_agent("terminal")
    assert slots.start("terminal")["reason_code"] == "invalid_transition"
    with pytest.raises(AgentSlotValidationError):
        AgentSlotRegistry(tmp_path / "slots.sqlite", capacity=5)


def test_spawn_exception_is_recorded_and_retried_with_same_slot(tmp_path: Path) -> None:
    slots = registry(tmp_path, retry_limit=1)
    calls = []

    def spawn(agent_id, record):
        calls.append((agent_id, record["attempt"]))
        if len(calls) == 1:
            raise RuntimeError("adapter unavailable")
        return {"success": True}

    result = slots.spawn_batch(["exception-retry"], spawn)
    assert result["results"][0]["success"] is True
    assert result["results"][0]["error"] == "adapter unavailable"
    assert calls == [("exception-retry", 1), ("exception-retry", 2)]
