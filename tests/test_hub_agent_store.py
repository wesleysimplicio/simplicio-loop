from __future__ import annotations

import pytest

from simplicio_loop.hub_agent_store import (
    HubAgentStore,
    LegacyStoreRemoved,
    ValidationError,
    build_job,
    build_receipt,
    validate_job,
    validate_receipt,
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


def test_legacy_store_instantiation_fails_closed_without_creating_a_path(tmp_path):
    path = tmp_path / "agent.sqlite"
    with pytest.raises(LegacyStoreRemoved):
        HubAgentStore(path)
    assert not path.exists()


def test_job_and_receipt_contracts_remain_pure_and_hash_bound():
    record = job()
    assert validate_job(record) == record
    receipt = build_receipt(
        job_id="job-1", generation=1, fence="fence-1", terminal_state="succeeded",
        outcome={"exit_code": 0}, evidence_hashes=["c" * 64],
    )
    assert validate_receipt(receipt, job_id="job-1", generation=1, fence="fence-1", terminal_state="succeeded") == receipt


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


def test_receipt_validation_rejects_wrong_identity_and_non_terminal_state():
    receipt = build_receipt(
        job_id="job-1", generation=1, fence="fence-1", terminal_state="failed",
        outcome={"error": "safe"}, evidence_hashes=[],
    )
    with pytest.raises(ValidationError):
        validate_receipt(receipt, job_id="job-2", generation=1, fence="fence-1", terminal_state="failed")
    with pytest.raises(ValidationError):
        build_receipt(job_id="job-1", generation=1, fence="fence-1", terminal_state="running", outcome={}, evidence_hashes=[])
