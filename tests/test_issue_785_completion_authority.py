from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from simplicio_loop.work_gap_ledger import (
    LedgerError,
    WorkGap,
    WorkGapLedger,
    sha256_evidence,
    validate_work_gap_snapshot,
)


def _evidence(kind, actor):
    return sha256_evidence(kind, f"evidence:{kind}", kind.encode(), actor)


def _complete(ledger, gap, *, commit="commit-main"):
    ledger.assign_owner(
        gap.key, owner_project="simplicio-loop", owner_agent="owner",
        actor_id="coverage-seat",
    )
    ledger.transition(gap.key, "PLANNED", actor_id="planner-seat", seat="planner")
    ledger.transition(
        gap.key, "IMPLEMENTED", actor_id="executor-seat", seat="executor",
        executor_id="executor-seat", expected_revision=commit,
        evidence=(_evidence("implementation", "executor-seat"),),
    )
    ledger.transition(
        gap.key, "VERIFIED", actor_id="verifier-seat", seat="verifier",
        verifier_id="verifier-seat",
        evidence=(_evidence("verification", "verifier-seat"),),
    )
    ledger.transition(
        gap.key, "INTEGRATED", actor_id="integration-seat", seat="integration",
        evidence=(_evidence("integration", "integration-seat"),),
    )
    ledger.transition(
        gap.key, "DELIVERED", actor_id="completion-seat", seat="completion",
        completion_auditor_id="completion-seat",
        evidence=(_evidence("delivery", "completion-seat"),),
        installed_artifact={
            "expected_commit": commit,
            "installed_commit": commit,
            "sha256": "a" * 64,
            "match": True,
        },
        source_requery={"commit": commit, "state": "merged"},
    )


def _gap(number=1, *, dependencies=()):
    return WorkGap(
        "REQ-785", f"AC-{number:04d}",
        dependencies=tuple(dependencies),
        expected_evidence=("implementation", "verification", "integration", "delivery"),
        delivery_target="package:simplicio-loop",
        expected_revision="commit-main",
    )


def test_delivery_fails_without_installed_artifact_requery():
    ledger = WorkGapLedger()
    gap = _gap()
    ledger.register(gap)
    ledger.assign_owner(
        gap.key, owner_project="simplicio-loop", owner_agent="owner",
        actor_id="coverage-seat",
    )
    ledger.transition(gap.key, "PLANNED", actor_id="planner-seat", seat="planner")
    ledger.transition(
        gap.key, "IMPLEMENTED", actor_id="executor-seat", seat="executor",
        executor_id="executor-seat",
        evidence=(_evidence("implementation", "executor-seat"),),
    )
    ledger.transition(
        gap.key, "VERIFIED", actor_id="verifier-seat", seat="verifier",
        verifier_id="verifier-seat",
        evidence=(_evidence("verification", "verifier-seat"),),
    )
    ledger.transition(
        gap.key, "INTEGRATED", actor_id="integration-seat", seat="integration",
        evidence=(_evidence("integration", "integration-seat"),),
    )
    with pytest.raises(LedgerError, match="installed artifact"):
        ledger.transition(
            gap.key, "DELIVERED", actor_id="completion-seat", seat="completion",
            completion_auditor_id="completion-seat",
            evidence=(_evidence("delivery", "completion-seat"),),
        )


def test_exactly_one_mutating_owner_and_orphan_explain():
    ledger = WorkGapLedger()
    gap = _gap()
    ledger.register(gap)
    assert ledger.explain(gap.key)["blockers"] == ["owner_missing"]
    ledger.assign_owner(
        gap.key, owner_project="simplicio-loop", owner_agent="owner-a",
        actor_id="coverage-seat",
    )
    with pytest.raises(LedgerError, match="ownership can only"):
        ledger.assign_owner(
            gap.key, owner_project="simplicio-loop", owner_agent="owner-b",
            actor_id="coverage-seat",
        )
    with pytest.raises(LedgerError, match="already registered"):
        ledger.register(gap)


def test_stale_package_or_source_commit_fails_closed():
    for installed, source, message in (
        ("stale", "commit-main", "installed artifact commit mismatch"),
        ("commit-main", "stale", "source re-query commit mismatch"),
    ):
        ledger = WorkGapLedger()
        gap = _gap()
        ledger.register(gap)
        ledger.assign_owner(
            gap.key, owner_project="simplicio-loop", owner_agent="owner",
            actor_id="coverage-seat",
        )
        ledger.transition(gap.key, "PLANNED", actor_id="planner-seat", seat="planner")
        ledger.transition(
            gap.key, "IMPLEMENTED", actor_id="executor-seat", seat="executor",
            executor_id="executor-seat",
            evidence=(_evidence("implementation", "executor-seat"),),
        )
        ledger.transition(
            gap.key, "VERIFIED", actor_id="verifier-seat", seat="verifier",
            verifier_id="verifier-seat",
            evidence=(_evidence("verification", "verifier-seat"),),
        )
        ledger.transition(
            gap.key, "INTEGRATED", actor_id="integration-seat", seat="integration",
            evidence=(_evidence("integration", "integration-seat"),),
        )
        with pytest.raises(LedgerError, match=message):
            ledger.transition(
                gap.key, "DELIVERED", actor_id="completion-seat", seat="completion",
                completion_auditor_id="completion-seat",
                evidence=(_evidence("delivery", "completion-seat"),),
                installed_artifact={
                    "expected_commit": "commit-main", "installed_commit": installed,
                    "sha256": "a" * 64, "match": True,
                },
                source_requery={"commit": source, "state": "merged"},
            )


def test_open_pr_source_state_does_not_equal_delivered():
    ledger = WorkGapLedger()
    gap = _gap()
    ledger.register(gap)
    ledger.assign_owner(
        gap.key, owner_project="simplicio-loop", owner_agent="owner",
        actor_id="coverage-seat",
    )
    ledger.transition(gap.key, "PLANNED", actor_id="planner-seat", seat="planner")
    ledger.transition(
        gap.key, "IMPLEMENTED", actor_id="executor-seat", seat="executor",
        executor_id="executor-seat",
        evidence=(_evidence("implementation", "executor-seat"),),
    )
    ledger.transition(
        gap.key, "VERIFIED", actor_id="verifier-seat", seat="verifier",
        verifier_id="verifier-seat",
        evidence=(_evidence("verification", "verifier-seat"),),
    )
    ledger.transition(
        gap.key, "INTEGRATED", actor_id="integration-seat", seat="integration",
        evidence=(_evidence("integration", "integration-seat"),),
    )
    with pytest.raises(LedgerError, match="source re-query is not terminal"):
        ledger.transition(
            gap.key, "DELIVERED", actor_id="completion-seat", seat="completion",
            completion_auditor_id="completion-seat",
            evidence=(_evidence("delivery", "completion-seat"),),
            installed_artifact={
                "expected_commit": "commit-main",
                "installed_commit": "commit-main",
                "sha256": "a" * 64, "match": True,
            },
            source_requery={"commit": "commit-main", "state": "open"},
        )


def test_replay_restart_has_same_digest_and_tamper_is_rejected():
    ledger = WorkGapLedger()
    gap = _gap()
    ledger.register(gap)
    _complete(ledger, gap)
    snapshot = ledger.snapshot()
    restarted = json.loads(json.dumps(snapshot))
    validation = validate_work_gap_snapshot(restarted)
    assert validation["ok"], validation["detail"]["errors"]
    assert validation["detail"]["digest"] == ledger.digest()

    restarted["events"][2]["actor_id"] = "forged-seat"
    tampered = validate_work_gap_snapshot(restarted)
    assert not tampered["ok"]
    assert any(
        "hash mismatch" in error or "invalid authority" in error
        for error in tampered["detail"]["errors"]
    )


def test_regression_invalidates_transitive_dependents_and_explain_is_exact():
    ledger = WorkGapLedger()
    root = _gap(1)
    dependent = _gap(2, dependencies=(root.key,))
    ledger.register(root)
    ledger.register(dependent)
    _complete(ledger, root)
    _complete(ledger, dependent)
    events = ledger.regress(
        root.key, actor_id="completion-seat",
        evidence=(_evidence("regression", "completion-seat"),),
    )
    assert len(events) == 2
    assert ledger.gaps[root.key].state == "REGRESSED"
    assert ledger.gaps[dependent.key].state == "REGRESSED"
    explanation = ledger.explain(dependent.key)
    assert explanation["terminal"] is False
    assert explanation["blockers"] == [
        f"dependency:{root.key}:REGRESSED", "regression_open"
    ]
    assert explanation["event_sequences"]
    assert explanation["ledger_digest"] == ledger.digest()


def test_dependency_cycle_is_rejected_by_snapshot_auditor():
    first = _gap(1, dependencies=("REQ-785:AC-0002",))
    second = _gap(2, dependencies=(first.key,))
    snapshot = {
        "schema": "simplicio.work-gap-ledger/v1",
        "gaps": [first.as_dict(), second.as_dict()],
        "events": [],
    }
    result = validate_work_gap_snapshot(snapshot)
    assert not result["ok"]
    assert any("dependency cycle" in error for error in result["detail"]["errors"])


def test_forged_direct_delivery_transition_is_rejected_on_replay():
    ledger = WorkGapLedger()
    gap = _gap()
    ledger.register(gap)
    _complete(ledger, gap)
    snapshot = ledger.snapshot()
    first = snapshot["events"][0]
    first["to_state"] = "DELIVERED"
    result = validate_work_gap_snapshot(snapshot)
    assert not result["ok"]
    assert any("illegal transition" in error for error in result["detail"]["errors"])


@pytest.mark.parametrize("count", [1, 20, 100, 600])
def test_no_work_item_loss_at_required_scales(count):
    ledger = WorkGapLedger()
    for number in range(count):
        gap = _gap(number)
        ledger.register(gap)
        _complete(ledger, gap)
    snapshot = ledger.snapshot()
    assert len(snapshot["gaps"]) == count
    assert len(snapshot["events"]) == count * 6
    assert not ledger.unresolved()
    validation = validate_work_gap_snapshot(snapshot)
    assert validation["ok"], validation["detail"]["errors"][:3]
    assert validation["detail"]["gap_count"] == count


def test_checked_in_stress_receipt_is_measured_replayable_and_model_free():
    path = Path(__file__).parent / "fixtures" / "work_gap_ledger_stress_785.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    declared = payload.pop("receipt_hash")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    assert declared == "sha256:" + hashlib.sha256(canonical).hexdigest()
    assert payload["classification"] == "MEASURED_LOCAL"
    assert payload["local_llm"] is False
    assert [row["items"] for row in payload["rows"]] == [1, 20, 100, 600]
    assert all(row["lost_items"] == 0 for row in payload["rows"])
    assert all(
        row["ledger_digest"] == row["replay_digest"] for row in payload["rows"]
    )
    assert validate_work_gap_snapshot(payload["clean_control"])["ok"]
