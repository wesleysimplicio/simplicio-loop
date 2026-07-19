from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from simplicio_loop.stage_agents import StageAgentError, receipt_fingerprint, receipt_integrity_hash, reduce_receipts

ROOT = Path(__file__).parents[1]
GRAPH = json.loads((ROOT / "contracts/stage-agents/v1/stages.json").read_text())
NOW = datetime(2026, 7, 16, 0, 1, tzinfo=timezone.utc)


def pair(stage_id, receipt_id):
    role = next(stage["role_id"] for stage in GRAPH["stages"] if stage["stage_id"] == stage_id)
    required = next(stage["required_capabilities"] for stage in GRAPH["stages"] if stage["stage_id"] == stage_id)
    instance = {"schema": "simplicio.agent-instance/v1", "agent_instance_id": "agent-" + stage_id, "role_id": role, "role_version": "1.0.0", "stage_id": stage_id, "stage_version": "1.0.0", "run_id": "run", "task_id": "task", "work_item_id": "work-item", "attempt_id": "attempt", "attempt_ordinal": 1, "fence": "fence", "plan_revision": 0, "runtime": "python", "provider": "local", "model": "none", "driver": "portable", "parent_agent_id": "coord", "coordinator_agent_id": "coord", "parent_instance_id": "parent", "idempotency_key": "idem-" + stage_id, "isolation_level": "process", "negotiated_capabilities": required, "context_hash": "0" * 64, "manifest_hash": GRAPH["manifest_hash"], "created_at": "2026-07-16T00:00:00Z", "ready_at": "2026-07-16T00:00:01Z", "started_at": "2026-07-16T00:00:02Z", "ended_at": "2026-07-16T00:00:03Z", "terminal_status": "completed", "reason_code": "ok"}
    receipt = {key: instance[key] for key in ("agent_instance_id", "role_id", "stage_id", "run_id", "task_id", "attempt_id", "attempt_ordinal", "fence", "plan_revision", "context_hash", "manifest_hash")}
    receipt.update({"schema": "simplicio.stage-receipt/v1", "receipt_id": receipt_id, "created_at": "2026-07-16T00:00:04Z", "observed_at": "2026-07-16T00:00:05Z", "ttl_seconds": 3600, "verdict": "pass", "evidence_refs": ["evidence://" + stage_id], "accepted": True, "reason_code": "ok", "input_hash": "2" * 64, "output_hash": "3" * 64, "integrity_hash": "0" * 64, "previous_receipt_hashes": [], "covered_acceptance_criteria": ["AC-test"], "commands": ["pytest -q"], "exit_codes": {"pytest -q": 0}, "artifact_refs": ["artifact://" + stage_id], "next_stage_recommendation": "none"})
    receipt["integrity_hash"] = receipt_integrity_hash(receipt)
    return receipt, instance


def test_reducer_requires_dependencies_and_releases_next_stage():
    intake, intake_instance = pair("intake", "r-intake")
    result = reduce_receipts(GRAPH, [intake], [intake_instance], now=NOW)
    assert result["completed_stages"] == ["intake"]
    assert result["released_stages"] == ["planning"]
    assert result["terminal"] is False
    planning, planning_instance = pair("planning", "r-planning")
    with pytest.raises(StageAgentError, match="unaccepted dependencies"):
        reduce_receipts(GRAPH, [planning], [planning_instance], now=NOW)


def test_reducer_replay_is_idempotent_and_conflicting_receipt_is_rejected():
    intake, instance = pair("intake", "r-intake")
    assert reduce_receipts(GRAPH, [intake, intake], [instance, instance], now=NOW)["receipt_count"] == 1
    changed = dict(intake, receipt_id="r-other")
    changed["integrity_hash"] = receipt_integrity_hash(changed)
    with pytest.raises(StageAgentError, match="multiple receipts"):
        reduce_receipts(GRAPH, [intake, changed], [instance, instance], now=NOW)
    changed = dict(intake, evidence_refs=["evidence://tampered"])
    changed["integrity_hash"] = receipt_integrity_hash(changed)
    with pytest.raises(StageAgentError, match="immutable content"):
        reduce_receipts(GRAPH, [intake, changed], [instance, instance], now=NOW)


def test_reducer_rejects_attempt_owner_change():
    intake, intake_instance = pair("intake", "r-intake-owner")
    changed, changed_instance = pair("intake", "r-intake-owner-change")
    changed_instance["agent_instance_id"] = "agent-intake-replacement"
    changed["agent_instance_id"] = changed_instance["agent_instance_id"]
    changed["integrity_hash"] = receipt_integrity_hash(changed)
    with pytest.raises(StageAgentError, match="conflicting agent instance"):
        reduce_receipts(GRAPH, [intake, changed], [intake_instance, changed_instance], now=NOW)


def test_reducer_allows_only_a_new_attempt_to_retry_with_a_new_agent():
    timed_out, timed_out_instance = pair("intake", "r-intake-timeout")
    timed_out["verdict"] = "timed_out"
    timed_out["accepted"] = False
    timed_out["rejection_reason"] = "deadline elapsed"
    timed_out_instance["terminal_status"] = "timed_out"
    timed_out["integrity_hash"] = receipt_integrity_hash(timed_out)
    retry, retry_instance = pair("intake", "r-intake-retry")
    retry["attempt_id"] = retry_instance["attempt_id"] = "attempt-2"
    retry["attempt_ordinal"] = retry_instance["attempt_ordinal"] = 2
    retry["agent_instance_id"] = retry_instance["agent_instance_id"] = "agent-intake-retry"
    retry["integrity_hash"] = receipt_integrity_hash(retry)
    result = reduce_receipts(GRAPH, [timed_out, retry], [timed_out_instance, retry_instance], now=NOW)
    assert result["completed_stages"] == ["intake"]


def test_reducer_rejects_retry_owner_reuse_and_budget_overflow():
    failed, failed_instance = pair("intake", "r-intake-failed")
    failed["verdict"] = "fail"
    failed["accepted"] = False
    failed["rejection_reason"] = "worker failed"
    failed_instance["terminal_status"] = "failed"
    failed["integrity_hash"] = receipt_integrity_hash(failed)
    reused, reused_instance = pair("intake", "r-intake-reused")
    reused["attempt_id"] = reused_instance["attempt_id"] = "attempt-2"
    reused["attempt_ordinal"] = reused_instance["attempt_ordinal"] = 2
    reused["integrity_hash"] = receipt_integrity_hash(reused)
    with pytest.raises(StageAgentError, match="distinct agent instance"):
        reduce_receipts(GRAPH, [failed, reused], [failed_instance, reused_instance], now=NOW)

    retry, retry_instance = pair("intake", "r-intake-retry-budget")
    retry["attempt_id"] = retry_instance["attempt_id"] = "attempt-2"
    retry["attempt_ordinal"] = retry_instance["attempt_ordinal"] = 2
    retry["agent_instance_id"] = retry_instance["agent_instance_id"] = "agent-intake-retry"
    retry["verdict"] = "fail"
    retry["accepted"] = False
    retry["rejection_reason"] = "worker failed again"
    retry_instance["terminal_status"] = "failed"
    retry["integrity_hash"] = receipt_integrity_hash(retry)
    overflow, overflow_instance = pair("intake", "r-intake-overflow")
    overflow["attempt_id"] = overflow_instance["attempt_id"] = "attempt-3"
    overflow["attempt_ordinal"] = overflow_instance["attempt_ordinal"] = 3
    overflow["agent_instance_id"] = overflow_instance["agent_instance_id"] = "agent-intake-overflow"
    overflow["verdict"] = "fail"
    overflow["accepted"] = False
    overflow["rejection_reason"] = "budget exhausted"
    overflow_instance["terminal_status"] = "failed"
    overflow["integrity_hash"] = receipt_integrity_hash(overflow)
    with pytest.raises(StageAgentError, match="retry budget exceeded"):
        reduce_receipts(GRAPH, [failed, retry, overflow], [failed_instance, retry_instance, overflow_instance], now=NOW)


def test_reducer_rejects_unbound_dependency_hash():
    intake, intake_instance = pair("intake", "r-intake-bound")
    planning, planning_instance = pair("planning", "r-planning-unbound")
    with pytest.raises(StageAgentError, match="previous receipt hashes"):
        reduce_receipts(GRAPH, [intake, planning], [intake_instance, planning_instance], now=NOW)


def test_reducer_rejects_nonterminal_instance_and_missing_capability():
    intake, intake_instance = pair("intake", "r-intake-running")
    intake_instance["terminal_status"] = "running"
    intake_instance.pop("ended_at")
    with pytest.raises(StageAgentError, match="completed instance"):
        reduce_receipts(GRAPH, [intake], [intake_instance], now=NOW)

    intake, intake_instance = pair("intake", "r-intake-capability")
    intake_instance["negotiated_capabilities"] = ["claim"]
    with pytest.raises(StageAgentError, match="capabilities"):
        reduce_receipts(GRAPH, [intake], [intake_instance], now=NOW)


def test_reducer_rejects_cross_run_receipts_before_completion():
    intake, intake_instance = pair("intake", "r-intake-run")
    planning, planning_instance = pair("planning", "r-planning-other-run")
    planning["run_id"] = planning_instance["run_id"] = "other-run"
    planning["integrity_hash"] = receipt_integrity_hash(planning)
    with pytest.raises(StageAgentError, match="crosses run"):
        reduce_receipts(GRAPH, [intake, planning], [intake_instance, planning_instance], now=NOW)


def test_reducer_accepts_dependency_reversed_input_order():
    intake, intake_instance = pair("intake", "r-intake-order")
    planning, planning_instance = pair("planning", "r-planning-order")
    planning["previous_receipt_hashes"] = [receipt_integrity_hash(intake)]
    planning["integrity_hash"] = receipt_integrity_hash(planning)
    result = reduce_receipts(GRAPH, [planning, intake], [planning_instance, intake_instance], now=NOW)
    assert result["completed_stages"] == ["intake", "planning"]


def test_reducer_full_dag_requires_every_accepted_receipt():
    stage_ids = [stage["stage_id"] for stage in GRAPH["stages"]]
    pairs = [pair(stage_id, "r-full-" + stage_id) for stage_id in stage_ids]
    receipts = [receipt for receipt, _ in pairs]
    instances = [instance for _, instance in pairs]
    by_stage = {receipt["stage_id"]: receipt for receipt in receipts}
    for receipt in receipts:
        dependencies = next(stage["depends_on"] for stage in GRAPH["stages"] if stage["stage_id"] == receipt["stage_id"])
        receipt["previous_receipt_hashes"] = [receipt_integrity_hash(by_stage[dependency]) for dependency in dependencies]
        receipt["integrity_hash"] = receipt_integrity_hash(receipt)
    result = reduce_receipts(GRAPH, receipts, instances, now=NOW)
    assert result["completed_stages"] == sorted(stage_ids)
    assert result["terminal"] is True
    assert all(receipt["accepted"] is True and receipt["verdict"] == "pass" for receipt in receipts)

    missing = stage_ids.index("validating")
    with pytest.raises(StageAgentError, match="unaccepted dependencies"):
        reduce_receipts(GRAPH, receipts[:missing] + receipts[missing + 1:], instances[:missing] + instances[missing + 1:], now=NOW)

    tampered = dict(receipts[0], output_hash="f" * 64)
    with pytest.raises(StageAgentError, match="integrity"):
        reduce_receipts(GRAPH, [tampered] + receipts[1:], instances, now=NOW)

    cancelled = dict(receipts[0], verdict="cancelled", accepted=False, rejection_reason="cancelled")
    instances[0] = dict(instances[0], terminal_status="cancelled")
    cancelled["integrity_hash"] = receipt_integrity_hash(cancelled)
    with pytest.raises(StageAgentError, match="unaccepted dependencies"):
        reduce_receipts(GRAPH, [cancelled] + receipts[1:], instances, now=NOW)


def test_reducer_rejects_shared_actor_across_independent_roles():
    intake, intake_instance = pair("intake", "r-intake-shared")
    safety, safety_instance = pair("safety", "r-safety-shared")
    safety_instance["agent_instance_id"] = intake_instance["agent_instance_id"]
    safety["agent_instance_id"] = safety_instance["agent_instance_id"]
    safety["integrity_hash"] = receipt_integrity_hash(safety)
    with pytest.raises(StageAgentError, match="independence"):
        reduce_receipts(GRAPH, [intake, safety], [intake_instance, safety_instance], now=NOW)


def test_reducer_does_not_release_blocked_stage_or_dependents():
    blocked, blocked_instance = pair("intake", "r-intake-blocked")
    blocked["verdict"] = "blocked"
    blocked["accepted"] = False
    blocked["rejection_reason"] = "safety gate"
    blocked_instance["terminal_status"] = "blocked"
    blocked["integrity_hash"] = receipt_integrity_hash(blocked)
    result = reduce_receipts(GRAPH, [blocked], [blocked_instance], now=NOW)
    assert result["released_stages"] == []
    assert result["blocked_stages"] == ["intake"]


def test_reducer_blocked_stage_requires_explicit_superseding_pass():
    blocked, blocked_instance = pair("intake", "r-intake-blocked-lineage")
    blocked["verdict"] = "blocked"
    blocked["accepted"] = False
    blocked["rejection_reason"] = "human gate"
    blocked_instance["terminal_status"] = "blocked"
    blocked["integrity_hash"] = receipt_integrity_hash(blocked)
    retry, retry_instance = pair("intake", "r-intake-supersede")
    retry["attempt_id"] = retry_instance["attempt_id"] = "attempt-2"
    retry["attempt_ordinal"] = retry_instance["attempt_ordinal"] = 2
    retry["agent_instance_id"] = retry_instance["agent_instance_id"] = "agent-intake-supersede"
    retry["integrity_hash"] = receipt_integrity_hash(retry)
    with pytest.raises(StageAgentError, match="explicitly supersede"):
        reduce_receipts(GRAPH, [blocked, retry], [blocked_instance, retry_instance], now=NOW)

    retry["supersedes_receipt_hash"] = blocked["integrity_hash"]
    retry["integrity_hash"] = receipt_integrity_hash(retry)
    result = reduce_receipts(GRAPH, [blocked, retry], [blocked_instance, retry_instance], now=NOW)
    assert result["completed_stages"] == ["intake"]


def test_reducer_rejects_noncontiguous_attempt_ordinal():
    failed, failed_instance = pair("intake", "r-intake-ordinal-1")
    failed["verdict"] = "fail"
    failed["accepted"] = False
    failed["rejection_reason"] = "transient"
    failed_instance["terminal_status"] = "failed"
    failed["integrity_hash"] = receipt_integrity_hash(failed)
    retry, retry_instance = pair("intake", "r-intake-ordinal-3")
    retry["attempt_id"] = retry_instance["attempt_id"] = "attempt-3"
    retry["attempt_ordinal"] = retry_instance["attempt_ordinal"] = 3
    retry["agent_instance_id"] = retry_instance["agent_instance_id"] = "agent-intake-ordinal-3"
    retry["integrity_hash"] = receipt_integrity_hash(retry)
    with pytest.raises(StageAgentError, match="ordinal lineage"):
        reduce_receipts(GRAPH, [failed, retry], [failed_instance, retry_instance], now=NOW)


def test_reducer_rejects_incomplete_instance():
    intake, instance = pair("intake", "r-invalid-instance")
    instance.pop("runtime")
    with pytest.raises(StageAgentError, match="instance rejected"):
        reduce_receipts(GRAPH, [intake], [instance], now=NOW)


def test_receipt_id_is_global_and_cannot_cross_stages():
    intake, intake_instance = pair("intake", "same-id")
    planning, planning_instance = pair("planning", "same-id")
    planning["integrity_hash"] = receipt_integrity_hash(planning)
    with pytest.raises(StageAgentError, match="immutable content"):
        reduce_receipts(GRAPH, [intake, planning], [intake_instance, planning_instance], now=NOW)
