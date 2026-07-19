"""Unit tests for `scripts/agent_replication.py` (issue #469 MVP core): admission control
and winner selection for elastic agent replication.

Covers: admission denies on insufficient slots/budget, admission caps weakly-justified
(idle_capacity-only) requests instead of granting the full ask, admission admits normally
otherwise, winner selection picks the earliest-VERIFIED candidate (never the earliest-
responded one -- the "first_response_wins" anti-pattern the issue explicitly calls out),
tie-break by lowest cost, the no-verified-candidate case, and cancel_losers excluding the
winner while refusing (not raising) on an unknown winner_id.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from agent_replication import (  # noqa: E402 -- scripts/ on sys.path for this import
    PER_REPLICA_COST,
    cancel_losers,
    decide_admission,
    make_request,
    select_winner,
)


def _req(mode="replica_diverse_strategy", reason_code="slow_p95", requested=3, min_r=1,
          max_r=3, token_budget=1000):
    return make_request("r1", "t1", mode, reason_code, requested, min_r, max_r,
                        token_budget=token_budget)


# ----- admission -----------------------------------------------------------------------------

def test_admission_denies_insufficient_slots():
    req = _req(min_r=2, max_r=3, requested=3)
    d = decide_admission(req, available_slots=1, available_budget=10_000)
    assert d["admitted"] is False
    assert d["reason"] == "insufficient_slots"
    assert d["replicas"] == 0


def test_admission_denies_insufficient_budget():
    req = _req(min_r=2, max_r=3, requested=3)
    d = decide_admission(req, available_slots=10, available_budget=PER_REPLICA_COST)
    assert d["admitted"] is False
    assert d["reason"] == "insufficient_budget"
    assert d["replicas"] == 0


def test_admission_caps_idle_capacity_only_requests():
    req = _req(mode="portfolio", reason_code="idle_capacity", requested=5, min_r=1, max_r=5,
                token_budget=10_000)
    d = decide_admission(req, available_slots=5, available_budget=10_000)
    assert d["admitted"] is True
    assert d["replicas"] < req["requested_replicas"]
    assert d["replicas"] == 2  # min_replicas(1) + 1 hedge, not the full 5 requested
    assert d["reason"] == "admitted_capped_weak_justification"


def test_admission_idle_capacity_not_capped_below_min_replicas():
    # min_replicas itself is still respected -- the cap only limits the EXTRA above it.
    req = _req(mode="hot_standby", reason_code="idle_capacity", requested=1, min_r=1, max_r=1,
                token_budget=10_000)
    d = decide_admission(req, available_slots=10, available_budget=10_000)
    assert d["admitted"] is True
    assert d["replicas"] == 1


def test_admission_admits_normally_with_strong_reason_code():
    req = _req(reason_code="high_variance", requested=3, min_r=1, max_r=3, token_budget=1000)
    d = decide_admission(req, available_slots=10, available_budget=1000)
    assert d["admitted"] is True
    assert d["reason"] == "admitted"
    assert d["replicas"] == 3  # min(requested, max_replicas, slots, affordable)


def test_admission_bounded_by_available_slots_and_budget():
    req = _req(reason_code="sla_risk", requested=5, min_r=1, max_r=5, token_budget=1000)
    d = decide_admission(req, available_slots=2, available_budget=1000)
    assert d["admitted"] is True
    assert d["replicas"] == 2  # slots is the binding constraint

    d2 = decide_admission(req, available_slots=10, available_budget=2 * PER_REPLICA_COST)
    assert d2["admitted"] is True
    assert d2["replicas"] == 2  # budget is the binding constraint


def test_make_request_rejects_unknown_mode_or_reason_code():
    import pytest
    with pytest.raises(ValueError):
        make_request("r", "t", "not_a_real_mode", "slow_p95", 1, 1, 1)
    with pytest.raises(ValueError):
        make_request("r", "t", "shard", "not_a_real_reason", 1, 1, 1)


# ----- winner selection ------------------------------------------------------------------------

def test_first_response_does_not_win_first_verified_does():
    """The core anti-pattern the issue calls out: a candidate that responded FIRST but was
    never verified must lose to a LATER candidate that WAS verified."""
    candidates = [
        {"replica_id": "fast_unverified", "responded_at": 1.0, "verified": False,
         "verified_at": None, "cost": 1.0},
        {"replica_id": "slower_verified", "responded_at": 5.0, "verified": True,
         "verified_at": 6.0, "cost": 2.0},
    ]
    verdict = select_winner(candidates)
    assert verdict["winner"] == "slower_verified"
    assert verdict["reason"] == "first_verified_candidate_wins"


def test_winner_selection_tie_breaks_by_lowest_cost():
    candidates = [
        {"replica_id": "cheap", "responded_at": 1.0, "verified": True, "verified_at": 3.0,
         "cost": 1.0},
        {"replica_id": "pricey", "responded_at": 2.0, "verified": True, "verified_at": 3.0,
         "cost": 5.0},
    ]
    assert select_winner(candidates)["winner"] == "cheap"


def test_winner_selection_no_verified_candidate():
    candidates = [{"replica_id": "a", "responded_at": 1.0, "verified": False,
                   "verified_at": None, "cost": 1.0}]
    verdict = select_winner(candidates)
    assert verdict["winner"] is None
    assert verdict["reason"] == "no_verified_candidate"


def test_winner_selection_empty_candidates():
    verdict = select_winner([])
    assert verdict["winner"] is None
    assert verdict["reason"] == "no_verified_candidate"


def test_winner_selection_earliest_verified_among_several():
    candidates = [
        {"replica_id": "a", "responded_at": 1.0, "verified": True, "verified_at": 10.0,
         "cost": 1.0},
        {"replica_id": "b", "responded_at": 2.0, "verified": True, "verified_at": 4.0,
         "cost": 9.0},
        {"replica_id": "c", "responded_at": 0.5, "verified": False, "verified_at": None,
         "cost": 0.1},
    ]
    assert select_winner(candidates)["winner"] == "b"


# ----- cancel_losers ---------------------------------------------------------------------------

def test_cancel_losers_excludes_winner():
    candidates = [
        {"replica_id": "a", "responded_at": 1.0, "verified": True, "verified_at": 1.0,
         "cost": 1.0},
        {"replica_id": "b", "responded_at": 2.0, "verified": True, "verified_at": 2.0,
         "cost": 1.0},
        {"replica_id": "c", "responded_at": 3.0, "verified": False, "verified_at": None,
         "cost": 1.0},
    ]
    result = cancel_losers(candidates, "a")
    assert result["ok"] is True
    assert result["error"] is None
    assert sorted(result["cancel"]) == ["b", "c"]


def test_cancel_losers_refuses_unknown_winner_without_raising():
    candidates = [{"replica_id": "a", "responded_at": 1.0, "verified": True,
                   "verified_at": 1.0, "cost": 1.0}]
    result = cancel_losers(candidates, "does_not_exist")
    assert result["ok"] is False
    assert result["error"] == "unknown_winner_id"
    assert result["cancel"] == []
