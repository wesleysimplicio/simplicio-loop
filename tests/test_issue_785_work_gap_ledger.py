from __future__ import annotations

import pytest

from simplicio_loop.work_gap_ledger import (
    LedgerError,
    WorkGap,
    WorkGapLedger,
    sha256_evidence,
)


def _gap(*, dependencies=()):
    return WorkGap(
        requirement_id="REQ-785",
        acceptance_criterion_id="AC-1",
        dependencies=tuple(dependencies),
        expected_evidence=("implementation", "verification", "integration", "delivery"),
        delivery_target="package:simplicio-loop",
        expected_revision="commit-abc",
    )


def _advance_to_implemented(ledger: WorkGapLedger, key: str) -> None:
    ledger.assign_owner(
        key, owner_project="simplicio-loop", owner_agent="stage-agent", actor_id="coverage-1"
    )
    ledger.transition(key, "PLANNED", actor_id="planner-1", seat="planner")
    ledger.transition(
        key,
        "IMPLEMENTED",
        actor_id="executor-1",
        seat="executor",
        executor_id="executor-1",
        evidence=(sha256_evidence("implementation", "commit:abc", b"patch", "executor-1"),),
        expected_revision="commit-abc",
    )


def test_orphan_cannot_skip_to_planned():
    ledger = WorkGapLedger()
    gap = _gap()
    ledger.register(gap)
    with pytest.raises(LedgerError, match="illegal transition"):
        ledger.transition(gap.key, "PLANNED", actor_id="planner-1", seat="planner")


def test_executor_cannot_self_verify():
    ledger = WorkGapLedger()
    gap = _gap()
    ledger.register(gap)
    _advance_to_implemented(ledger, gap.key)
    with pytest.raises(LedgerError, match="independent"):
        ledger.transition(
            gap.key,
            "VERIFIED",
            actor_id="executor-1",
            seat="verifier",
            verifier_id="executor-1",
            evidence=(sha256_evidence("verification", "test:1", b"ok", "executor-1"),),
        )


def test_dependency_blocks_integration_until_terminal():
    ledger = WorkGapLedger()
    dependency = WorkGap(
        requirement_id="REQ-DEP",
        acceptance_criterion_id="AC-1",
        expected_evidence=("implementation", "verification", "integration", "delivery"),
        delivery_target="package:dependency",
    )
    gap = _gap(dependencies=(dependency.key,))
    ledger.register(dependency)
    ledger.register(gap)
    _advance_to_implemented(ledger, gap.key)
    ledger.transition(
        gap.key,
        "VERIFIED",
        actor_id="verifier-1",
        seat="verifier",
        verifier_id="verifier-1",
        evidence=(sha256_evidence("verification", "test:1", b"ok", "verifier-1"),),
    )
    with pytest.raises(LedgerError, match="not terminal"):
        ledger.transition(
            gap.key,
            "INTEGRATED",
            actor_id="integrator-1",
            seat="integrator",
            evidence=(sha256_evidence("integration", "e2e:1", b"ok", "integrator-1"),),
        )


def test_three_independent_seats_deliver_and_digest_is_deterministic():
    ledger = WorkGapLedger()
    gap = _gap()
    ledger.register(gap)
    _advance_to_implemented(ledger, gap.key)
    ledger.transition(
        gap.key,
        "VERIFIED",
        actor_id="verifier-1",
        seat="verifier",
        verifier_id="verifier-1",
        evidence=(sha256_evidence("verification", "test:1", b"ok", "verifier-1"),),
    )
    ledger.transition(
        gap.key,
        "INTEGRATED",
        actor_id="integrator-1",
        seat="integrator",
        evidence=(sha256_evidence("integration", "e2e:1", b"ok", "integrator-1"),),
    )
    ledger.transition(
        gap.key,
        "DELIVERED",
        actor_id="auditor-1",
        seat="completion",
        completion_auditor_id="auditor-1",
        evidence=(sha256_evidence("delivery", "release:1", b"ok", "auditor-1"),),
        installed_artifact={
            "expected_commit": "commit-abc",
            "installed_commit": "commit-abc",
            "sha256": "a" * 64,
            "match": True,
        },
        source_requery={"commit": "commit-abc", "state": "merged"},
    )
    ledger.verify_chain()
    assert ledger.unresolved() == ()
    first = ledger.digest()
    assert first == ledger.digest()
    assert ledger.gaps[gap.key].state == "DELIVERED"


def test_event_tamper_is_rejected():
    ledger = WorkGapLedger()
    gap = _gap()
    ledger.register(gap)
    ledger.assign_owner(
        gap.key, owner_project="simplicio-loop", owner_agent="agent", actor_id="coverage"
    )
    ledger.events[0] = type(ledger.events[0])(
        **{**ledger.events[0].__dict__, "hash": "0" * 64}
    )
    with pytest.raises(LedgerError, match="hash mismatch"):
        ledger.verify_chain()
