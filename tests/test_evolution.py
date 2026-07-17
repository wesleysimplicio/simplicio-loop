"""Unit tests for scripts/evolution.py (the continuous-evolution coordinator MVP, GH #467).

Covers: the DEFECT GUARD (propose() refuses defect/regression instead of mislabeling them as
improvements), priority-score determinism + ordering, dedup-by-fingerprint, the per-run budget
cap, and the Evolution Ledger report counts. Mirrors the fixture style of
tests/test_claims_audit_unit.py — insert scripts/ onto sys.path and import the worker module
directly rather than shelling out, so failures point at a line number instead of a subprocess
stack trace.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import evolution  # noqa: E402


def _proposal(tmp_path, **overrides):
    base = {
        "class": "improvement",
        "component": "scripts/loop_progress.py",
        "problem": "status --json rescans the whole backlog every call (seen at line 10)",
        "benefit": "cache last render, fewer rescans",
        "impact": "medium",
        "effort": "low",
        "risk": "low",
    }
    base.update(overrides)
    path = str(tmp_path / "proposals.jsonl")
    return base, path


# ----- defect guard --------------------------------------------------------------------------

def test_defect_guard_refuses_defect_class(tmp_path):
    payload, path = _proposal(tmp_path, **{"class": "defect"})
    rec, created, error = evolution.propose(payload, path)
    assert rec is None
    assert created is False
    assert error
    assert "finding_collector.py" in error
    assert not os.path.exists(path)  # refusal must not write anything to the store


def test_defect_guard_refuses_regression_class(tmp_path):
    payload, path = _proposal(tmp_path, **{"class": "regression"})
    rec, created, error = evolution.propose(payload, path)
    assert rec is None
    assert created is False
    assert "findings" in error.lower()


def test_defect_guard_allows_genuine_improvement_classes(tmp_path):
    for klass in ("improvement", "evolution", "optimization", "hardening", "discovery",
                  "maintenance"):
        payload, path = _proposal(tmp_path, **{"class": klass,
                                                "problem": "problem specific to %s" % klass})
        rec, created, error = evolution.propose(payload, path)
        assert not error, "class=%r should be accepted, got error=%r" % (klass, error)
        assert created is True
        assert rec["class"] == klass


# ----- priority score: deterministic + explainable ordering ---------------------------------

def test_compute_priority_is_deterministic():
    a = evolution.compute_priority("high", "medium", "low")
    b = evolution.compute_priority("high", "medium", "low")
    assert a == b


def test_compute_priority_orders_higher_impact_above():
    low_impact = evolution.compute_priority("low", "low", "low")
    high_impact = evolution.compute_priority("high", "low", "low")
    assert high_impact > low_impact


def test_compute_priority_orders_lower_effort_above():
    low_effort = evolution.compute_priority("medium", "low", "low")
    high_effort = evolution.compute_priority("medium", "high", "low")
    assert low_effort > high_effort


def test_compute_priority_orders_lower_risk_above():
    low_risk = evolution.compute_priority("medium", "medium", "low")
    high_risk = evolution.compute_priority("medium", "medium", "high")
    assert low_risk > high_risk


def test_compute_priority_high_impact_low_effort_low_risk_beats_the_opposite():
    best = evolution.compute_priority("critical", "low", "low")
    worst = evolution.compute_priority("low", "high", "high")
    assert best > worst


def test_compute_priority_rejects_unknown_value():
    import pytest
    with pytest.raises(ValueError):
        evolution.compute_priority("not-a-level", "low", "low")


# ----- dedup by fingerprint -------------------------------------------------------------------

def test_repeat_proposal_updates_existing_instead_of_creating_new(tmp_path):
    payload, path = _proposal(tmp_path)
    rec1, created1, err1 = evolution.propose(payload, path)
    assert created1 is True and not err1
    assert rec1["occurrences"] == 1

    # same component + class + problem (line number differs but normalizes away) -> same
    # fingerprint -> update, not a second record
    repeat_payload, _ = _proposal(
        tmp_path,
        problem="status --json rescans the whole backlog every call (seen at line 99)",
        impact="high",
    )
    rec2, created2, err2 = evolution.propose(repeat_payload, path)
    assert not err2
    assert created2 is False
    assert rec2["proposal_id"] == rec1["proposal_id"]
    assert rec2["occurrences"] == 2
    assert rec2["priority_score"] == evolution.compute_priority("high", "low", "low")

    all_recs = evolution.list_proposals(path)
    assert len(all_recs) == 1


def test_distinct_problem_creates_a_second_proposal(tmp_path):
    payload, path = _proposal(tmp_path)
    rec1, _, _ = evolution.propose(payload, path)
    payload2, _ = _proposal(tmp_path, problem="a totally unrelated improvement opportunity",
                            component="scripts/other.py")
    rec2, created2, err2 = evolution.propose(payload2, path)
    assert not err2
    assert created2 is True
    assert rec2["proposal_id"] != rec1["proposal_id"]
    assert len(evolution.list_proposals(path)) == 2


# ----- budget cap ------------------------------------------------------------------------------

def test_budget_cap_refuses_a_new_fingerprint_past_the_limit(tmp_path):
    path = str(tmp_path / "proposals.jsonl")
    for word in ("alpha", "beta", "gamma"):
        payload, _ = _proposal(tmp_path, problem="distinct budget problem %s" % word,
                               **{"class": "maintenance"})
        rec, created, err = evolution.propose(payload, path, budget_max=3)
        assert created is True and not err
    assert len(evolution.list_proposals(path)) == 3

    over_payload, _ = _proposal(tmp_path, problem="one too many for the budget",
                                **{"class": "maintenance"})
    rec, created, err = evolution.propose(over_payload, path, budget_max=3)
    assert rec is None
    assert created is False
    assert "budget_exceeded" in err
    assert len(evolution.list_proposals(path)) == 3  # refusal must not grow the store


def test_budget_cap_allows_updating_an_existing_fingerprint_at_cap(tmp_path):
    path = str(tmp_path / "proposals.jsonl")
    for word in ("alpha", "beta", "gamma"):
        payload, _ = _proposal(tmp_path, problem="distinct budget problem %s" % word,
                               **{"class": "maintenance"})
        evolution.propose(payload, path, budget_max=3)

    update_payload, _ = _proposal(tmp_path, problem="distinct budget problem alpha",
                                  impact="high", **{"class": "maintenance"})
    rec, created, err = evolution.propose(update_payload, path, budget_max=3)
    assert not err
    assert created is False  # updating an existing fingerprint is not "growth"
    assert len(evolution.list_proposals(path)) == 3


# ----- report / Evolution Ledger ---------------------------------------------------------------

def test_report_counts_by_class_and_state(tmp_path):
    path = str(tmp_path / "proposals.jsonl")
    payload_a, _ = _proposal(tmp_path, **{"class": "improvement"})
    payload_b, _ = _proposal(tmp_path, problem="second distinct problem",
                             **{"class": "hardening"})
    payload_c, _ = _proposal(tmp_path, problem="third distinct problem",
                             **{"class": "improvement"})
    evolution.propose(payload_a, path)
    evolution.propose(payload_b, path)
    evolution.propose(payload_c, path)

    summary = evolution.report_summary(path)
    assert summary["ledger"] == "Evolution Ledger"
    assert summary["total"] == 3
    assert summary["by_class"]["improvement"] == 2
    assert summary["by_class"]["hardening"] == 1
    assert summary["by_state"]["observed"] == 3
    assert len(summary["top_by_priority"]) == 3
    scores = [t["priority_score"] for t in summary["top_by_priority"]]
    assert scores == sorted(scores, reverse=True)


def test_report_on_empty_store_is_clean(tmp_path):
    path = str(tmp_path / "does_not_exist.jsonl")
    summary = evolution.report_summary(path)
    assert summary["total"] == 0
    assert summary["by_class"] == {}
    assert summary["top_by_priority"] == []


# ----- doctor ------------------------------------------------------------------------------

def test_doctor_clean_store_has_no_issues(tmp_path):
    path = str(tmp_path / "proposals.jsonl")
    payload, _ = _proposal(tmp_path)
    evolution.propose(payload, path)
    result = evolution.doctor_check(path)
    assert result["ok"] is True
    assert result["issues"] == []


def test_doctor_missing_store_is_ok(tmp_path):
    result = evolution.doctor_check(str(tmp_path / "does_not_exist.jsonl"))
    assert result["ok"] is True


# ----- selftest still exercises the CLI-facing path end-to-end -------------------------------

def test_worker_selftest_passes():
    import subprocess
    r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "evolution.py"),
                        "selftest"], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "selftest: PASS" in r.stdout
