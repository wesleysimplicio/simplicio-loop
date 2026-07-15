"""Unit tests for simplicio_loop/freshness.py — the #290 TTL/freshness policy module.

"Unknown is not pass": a missing or unparsable `observed_at` must always be treated as
stale, never assumed fresh. `ttl_seconds<=0` forces "always stale" (used to force a live
re-query for a critical transition). Ages within the TTL are fresh; ages beyond it, or
timestamps implausibly far in the future (clock skew / fabrication), are stale.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simplicio_loop import freshness


def test_ttl_for_state_uses_documented_defaults():
    assert freshness.ttl_for_state("pr-open") == 300
    assert freshness.ttl_for_state("merge-ready") == 120
    assert freshness.ttl_for_state("merged") == 3600
    assert freshness.ttl_for_state("released") == 86400
    assert freshness.ttl_for_state("deployed") == 3600


def test_ttl_for_state_unknown_class_uses_fallback():
    assert freshness.ttl_for_state("some-unknown-class") == freshness.DEFAULT_FALLBACK_TTL_SECONDS


def test_ttl_for_state_override_wins():
    assert freshness.ttl_for_state("merged", overrides={"merged": 10}) == 10


def test_is_stale_missing_timestamp_is_always_stale():
    assert freshness.is_stale(None, 300) is True
    assert freshness.is_stale("", 300) is True


def test_is_stale_unparsable_timestamp_is_stale():
    assert freshness.is_stale("not-a-timestamp", 300) is True


def test_is_stale_within_ttl_is_fresh():
    now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    observed = (now - timedelta(seconds=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert freshness.is_stale(observed, 300, now=now) is False


def test_is_stale_past_ttl_is_stale():
    now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    observed = (now - timedelta(seconds=301)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert freshness.is_stale(observed, 300, now=now) is True


def test_is_stale_zero_or_negative_ttl_always_stale():
    now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    observed = now.strftime("%Y-%m-%dT%H:%M:%SZ")  # just observed
    assert freshness.is_stale(observed, 0, now=now) is True
    assert freshness.is_stale(observed, -5, now=now) is True


def test_is_stale_far_future_timestamp_is_treated_as_stale():
    now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    future = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert freshness.is_stale(future, 300, now=now) is True


def test_is_stale_small_clock_skew_tolerance_not_treated_as_stale_on_its_own():
    now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    slightly_future = (now + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert freshness.is_stale(slightly_future, 300, now=now) is False


def test_freshness_gate_pass_shape():
    now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    observed = (now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    gate = freshness.freshness_gate(observed, "pr-open", now=now)
    assert gate["status"] == "pass"
    assert gate["reason_code"] == "observation_fresh"
    assert gate["ttl_seconds"] == 300


def test_freshness_gate_fail_shape():
    now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    observed = (now - timedelta(seconds=999999)).strftime("%Y-%m-%dT%H:%M:%SZ")
    gate = freshness.freshness_gate(observed, "merged", now=now)
    assert gate["status"] == "fail"
    assert gate["reason_code"] == "observation_stale"


def test_parse_iso_accepts_both_z_and_offset_forms():
    now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    z_form = "2026-07-15T11:59:00Z"
    offset_form = "2026-07-15T11:59:00+00:00"
    assert freshness.is_stale(z_form, 300, now=now) == freshness.is_stale(offset_form, 300, now=now)
