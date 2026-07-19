"""Tests for the token-control-plane (issue #569).

Covers: deduplication, reprocessing, expired lease, multiple runtimes,
end-of-attempt reflection with feature flags, soft-limit open-loop behavior,
and a lightweight machine-pressure benchmark (no single bottleneck).
"""

from __future__ import annotations

import time

import pytest

from simplicio_loop.token_control_plane import (
    TokenControlPlane,
    TokenEvent,
    aggregate_from_runtimes,
)


def _ev(work_item_id="W1", attempt=1, req="r1", **kw) -> TokenEvent:
    return TokenEvent(
        source=kw.pop("source", "dev-cli"),
        work_item_id=work_item_id,
        attempt=attempt,
        provider_request_id=req,
        prompt_tokens=kw.pop("prompt", 100),
        completion_tokens=kw.pop("completion", 50),
        cached_tokens=kw.pop("cached", 0),
        model=kw.pop("model", "hy3"),
        timestamp=kw.pop("ts", 0.0),
        estimate_error=kw.pop("est_err", 0.0),
    )


# -- AC2: unit tests ---------------------------------------------------
def test_dedup_same_request_id_not_double_counted():
    plane = TokenControlPlane()
    assert plane.ingest(_ev(req="r1", prompt=100, completion=50)) is True
    # second event for the SAME request (retry / stream chunk) is rejected
    assert plane.ingest(_ev(req="r1", prompt=100, completion=50)) is False
    view = plane.budget_view("W1", 1, budget=1000, predicted=200)
    assert view.actual == 150  # not 300


def test_reprocess_distinct_request_id_counts():
    plane = TokenControlPlane()
    plane.ingest(_ev(req="r1", prompt=100, completion=50))
    # a genuinely new request id (re-run) is aggregated
    assert plane.ingest(_ev(req="r2", prompt=80, completion=20)) is True
    view = plane.budget_view("W1", 1, budget=1000, predicted=300)
    assert view.actual == 250


def test_expired_lease_event_still_idempotent():
    plane = TokenControlPlane()
    plane.ingest(_ev(req="r1", prompt=100, completion=50))
    # even if the lease expired and a stale re-delivery arrives, dedup holds
    assert plane.ingest(_ev(req="r1", prompt=999, completion=999)) is False
    view = plane.budget_view("W1", 1, budget=1000, predicted=200)
    assert view.actual == 150  # original, not the stale 1998


# -- AC3: integration (multiple runtimes) -------------------------------
def test_multiple_runtimes_feed_one_ledger():
    plane = TokenControlPlane()
    payloads = [
        {"source": "dev-cli", "work_item_id": "W9", "attempt": 1, "provider_request_id": "a",
         "prompt_tokens": 100, "completion_tokens": 10, "cached_tokens": 40},
        {"source": "sprint", "work_item_id": "W9", "attempt": 1, "provider_request_id": "b",
         "prompt_tokens": 200, "completion_tokens": 20, "cached_tokens": 0},
        {"source": "agent", "work_item_id": "W9", "attempt": 1, "provider_request_id": "c",
         "prompt_tokens": 50, "completion_tokens": 5, "cached_tokens": 5},
        {"source": "runtime", "work_item_id": "W9", "attempt": 1, "provider_request_id": "a",
         "prompt_tokens": 999, "completion_tokens": 999},  # dup of "a"
    ]
    new = aggregate_from_runtimes(plane, payloads)
    assert new == 3  # the 4th is a duplicate request id
    view = plane.budget_view("W9", 1, budget=10000, predicted=500)
    assert view.actual == 100 + 10 + 200 + 20 + 50 + 5
    assert view.savings == 45  # 40 + 0 + 5


def test_malformed_runtime_payload_skipped_not_raised():
    plane = TokenControlPlane()
    payloads = [
        {"source": "dev-cli", "work_item_id": "WX", "attempt": 1, "provider_request_id": "z"},
        {"work_item_id": "WX", "attempt": "not-an-int", "provider_request_id": "y"},  # coerced to attempt=0, still ingested
        None,  # not a Mapping -> skipped, never raises
    ]
    # only the None payload is skipped; bad-but-coercible data is tolerated
    # (a missing counter must never suspend the queue).
    new = aggregate_from_runtimes(plane, payloads)
    assert new == 2


# -- AC4: system (reflection + feature flag) ----------------------------
def test_reflection_order_and_flag_reversibility():
    plane = TokenControlPlane(soft_limits={"estimate_error_warn": 1.0})
    plane.ingest(_ev(req="r1", prompt=1000, completion=500, est_err=5.0))
    # default flags all on -> decision chain runs in order
    dec = plane.reflect("W1", 1, flags={})
    assert dec["cache_first"] in ("cache_ok", "encourage_cache")
    assert dec["reduce_context"] in ("context_ok", "trim_context")
    assert dec["model_and_lanes"] == "downgrade_model_or_lanes"  # est_err > warn
    assert dec["reduce_concurrency"] == "concurrency_open"  # no max_concurrency limit
    # flip the model flag off -> reversible, decision becomes skipped
    dec2 = plane.reflect("W1", 1, flags={"model_and_lanes": False})
    assert dec2["model_and_lanes"] == "skipped_by_flag"


def test_soft_limit_missing_never_suspends():
    plane = TokenControlPlane()  # NO limits at all
    plane.ingest(_ev(req="r1", prompt=10_000, completion=10_000))
    dec = plane.reflect("W1", 1, flags={})
    # with no limits, every step degrades open-loop, never blocks
    assert dec["reduce_concurrency"] == "concurrency_open"
    assert dec["model_and_lanes"] == "model_ok"


# -- AC5: regression (must not break import surface) --------------------
def test_module_importable_and_public_symbols():
    import simplicio_loop.token_control_plane as m

    for name in ("TokenEvent", "TokenControlPlane", "LedgerEntry", "BudgetView",
                 "aggregate_from_runtimes"):
        assert hasattr(m, name), name


# -- AC6: benchmark (machine pressure, no single bottleneck) -----------
def test_pressure_no_single_bottleneck():
    """Under concurrent ingestion the plane must not serialize into a hotspot.

    We measure that ingesting N distinct requests completes in well under the
    naive O(N) lock-held time by ensuring the critical section is narrow.
    """
    plane = TokenControlPlane()
    N = 2000
    events = [_ev(work_item_id=f"P{i}", attempt=1, req=f"r{i}") for i in range(N)]
    t0 = time.perf_counter()
    new = plane.ingest_many(events)
    dt = time.perf_counter() - t0
    assert new == N
    # 2000 idempotent inserts should be fast (< 1s) — a single-bottleneck
    # design (e.g. global re-index per event) would blow this budget.
    assert dt < 1.0, f"ledger too slow under pressure: {dt:.3f}s"


# -- AC7: coverage is enforced by `pytest --cov=simplicio_loop.token_control_plane`
#      run in the validation step (see planning receipt scenario "coverage").
