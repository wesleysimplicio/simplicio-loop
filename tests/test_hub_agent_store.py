from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from simplicio_loop.hub_agent_store import (
    HubAgentStore, IdempotencyConflict, TransitionConflict, ValidationError,
    build_job, build_receipt, validate_job, validate_receipt,
)


def job(**overrides):
    values = dict(
        idempotency_key="stage/run/task/1", graph_id="graph", run_id="run",
        task_id="task", stage_id="stage", role="implementer", attempt_id="attempt-1",
        source_fence="source:42", plan_revision="plan:7", input_hash="a" * 64,
        context_hash="b" * 64, process_spec={"argv": ["python", "agent.py"], "shell": False},
        deadline="2026-07-22T19:00:00Z", priority=10, resources={"cpu": 1, "memory_mb": 256},
    )
    values.update(overrides)
    return build_job(**values)


def move(store, record, target):
    handle = record["handle"]
    return store.transition(handle["job_id"], expected_state=record["state"],
                            generation=handle["generation"], fence=handle["fence"], target_state=target)


def test_full_state_machine_restart_and_atomic_terminal_receipt(tmp_path):
    path = tmp_path / "agent.sqlite"
    store = HubAgentStore(path)
    record, created = store.prepare(job())
    assert created and record["state"] == "prepared"
    stable = store.prepare(job())
    assert stable == (record, False)
    record = move(store, record, "queued")
    record = move(store, record, "leased")
    record = move(store, record, "running")
    handle = record["handle"]
    receipt = build_receipt(job_id=handle["job_id"], generation=handle["generation"],
                            fence=handle["fence"], terminal_state="succeeded",
                            outcome={"exit_code": 0}, evidence_hashes=["c" * 64])
    record = store.transition(handle["job_id"], expected_state="running", generation=1,
                              fence=handle["fence"], target_state="succeeded", receipt=receipt)
    reopened = HubAgentStore(path).get(handle["job_id"])
    assert reopened == record
    assert reopened["receipt"] == receipt
    assert reopened["job"]["process_spec"]["argv"] == ["python", "agent.py"]


def test_fencing_illegal_transitions_and_crash_window(tmp_path):
    store = HubAgentStore(tmp_path / "agent.sqlite")
    record, _ = store.prepare(job())
    with pytest.raises(TransitionConflict):
        move(store, record, "running")
    queued = move(store, record, "queued")
    leased = move(store, queued, "leased")
    old = leased["handle"]
    unknown = store.recover(old["job_id"], expected_state="leased", generation=1, fence=old["fence"])
    assert unknown["state"] == "recovery_unknown"
    assert unknown["handle"]["generation"] == 2 and unknown["handle"]["fence"] != old["fence"]
    with pytest.raises(TransitionConflict):
        store.transition(old["job_id"], expected_state="leased", generation=1,
                         fence=old["fence"], target_state="running")
    assert move(store, unknown, "queued")["state"] == "queued"


def test_idempotency_conflict_and_concurrent_replay(tmp_path):
    store = HubAgentStore(tmp_path / "agent.sqlite")
    first, _ = store.prepare(job())
    with pytest.raises(IdempotencyConflict):
        store.prepare(job(input_hash="d" * 64))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.prepare(job()), range(20)))
    assert {item[0]["handle"]["job_id"] for item in results} == {first["handle"]["job_id"]}
    assert not any(item[1] for item in results)


def test_read_validation_detects_job_and_terminal_tampering(tmp_path):
    path = tmp_path / "agent.sqlite"
    store = HubAgentStore(path)
    record, _ = store.prepare(job())
    with sqlite3.connect(str(path)) as db:
        payload = json.loads(db.execute("SELECT job_json FROM hub_agent_jobs").fetchone()[0])
        payload["role"] = "attacker"
        db.execute("UPDATE hub_agent_jobs SET job_json=?", (json.dumps(payload),))
    with pytest.raises(ValidationError):
        store.get(record["handle"]["job_id"])


@pytest.mark.parametrize("bad", [True, 1.2, "high"])
def test_priority_validation(bad):
    with pytest.raises(ValidationError):
        job(priority=bad)


def test_no_admitted_held_activation_or_process_execution_surface(tmp_path):
    store = HubAgentStore(tmp_path / "agent.sqlite")
    assert not hasattr(store, "dispatch")
    assert "admitted_held" not in __import__("simplicio_loop.hub_agent_store", fromlist=["STATES"]).STATES


@pytest.mark.parametrize("field,value", [
    ("graph_id", ""), ("source_fence", " bad"), ("input_hash", "ABC"),
    ("resources", []), ("process_spec", []),
    ("process_spec", {"argv": [], "shell": False}),
    ("process_spec", {"argv": ["echo"], "shell": True}),
])
def test_job_validation_failure_paths(field, value):
    with pytest.raises(ValidationError):
        job(**{field: value})
    with pytest.raises(ValidationError):
        validate_job([])


def test_receipt_validation_and_atomic_rollback(tmp_path):
    store = HubAgentStore(tmp_path / "agent.sqlite")
    record, _ = store.prepare(job())
    record = move(store, move(store, move(store, record, "queued"), "leased"), "running")
    handle = record["handle"]
    receipt = build_receipt(job_id=handle["job_id"], generation=1, fence=handle["fence"],
                            terminal_state="failed", outcome={"error": "safe"},
                            evidence_hashes=[])
    broken = dict(receipt, fence="stale")
    with pytest.raises(ValidationError):
        store.transition(handle["job_id"], expected_state="running", generation=1,
                         fence=handle["fence"], target_state="failed", receipt=broken)
    assert store.get(handle["job_id"])["state"] == "running"
    with pytest.raises(ValidationError):
        store.transition(handle["job_id"], expected_state="running", generation=1,
                         fence=handle["fence"], target_state="failed")
    with pytest.raises(ValidationError):
        validate_receipt([], job_id="x", generation=1, fence="f", terminal_state="failed")
    with pytest.raises(ValidationError):
        build_receipt(job_id="x", generation=0, fence="f", terminal_state="failed",
                      outcome={}, evidence_hashes=[])
    with pytest.raises(ValidationError):
        build_receipt(job_id="x", generation=1, fence="f", terminal_state="running",
                      outcome={}, evidence_hashes=[])


def test_missing_and_stale_recovery_paths(tmp_path):
    store = HubAgentStore(tmp_path / "agent.sqlite")
    with pytest.raises(KeyError):
        store.get("missing")
    with pytest.raises(KeyError):
        store.transition("missing", expected_state="prepared", generation=1, fence="f", target_state="queued")
    with pytest.raises(KeyError):
        store.recover("missing", expected_state="leased", generation=1, fence="f")
    record, _ = store.prepare(job())
    with pytest.raises(TransitionConflict):
        store.recover(record["handle"]["job_id"], expected_state="prepared", generation=1,
                      fence=record["handle"]["fence"])
    queued = move(store, record, "queued")
    leased = move(store, queued, "leased")
    with pytest.raises(TransitionConflict):
        store.recover(leased["handle"]["job_id"], expected_state="leased", generation=99,
                      fence=leased["handle"]["fence"])
