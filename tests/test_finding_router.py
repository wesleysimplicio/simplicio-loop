"""Unit + integration tests for simplicio_loop.finding_router (WI-466)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simplicio_loop import finding_router as rt  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    sp = tmp_path / "issue_routes.json"
    monkeypatch.setattr(rt, "LOCAL_STORE", sp)
    monkeypatch.setattr(rt, "_gh_available", lambda: False)  # force local fallback
    return sp


def test_route_creates_local_fallback_when_no_gh():
    res = rt.route_finding("operate", "reg-1", "high", "cli.py:1", True, item_id="WI-466")
    assert res.tracked is True
    assert res.created is True
    assert res.issue_ref.startswith("local:")


def test_dedup_same_fingerprint_no_duplicate():
    a = rt.route_finding("operate", "reg-1", "high", "cli.py:1", True, item_id="WI-466")
    b = rt.route_finding("operate", "reg-1", "high", "cli.py:1", True, item_id="WI-466")
    assert a.fingerprint == b.fingerprint
    assert a.issue_ref == b.issue_ref
    # only one route stored
    state = json.loads(rt.LOCAL_STORE.read_text(encoding="utf-8"))
    assert len(state) == 1


def test_untracked_problems_reported():
    rt.route_finding("survey", "dup-x", "medium", "m.py:9", True, item_id="WI-466")
    untracked = rt.untracked_problems(item_id="WI-466")
    assert len(untracked) == 1
    assert untracked[0]["finding_id"] == "dup-x"


def test_completion_blocked_true_when_untracked():
    rt.route_finding("survey", "blk-1", "high", "m.py:1", True, item_id="WI-466")
    # No real gh issue was created (local fallback) -> completion must be blocked.
    assert rt.completion_blocked(item_id="WI-466") is True


def test_completion_blocked_false_when_no_routes():
    assert rt.completion_blocked(item_id="WI-999") is False


def test_invalid_severity_rejected():
    with pytest.raises(ValueError):
        rt.route_finding("decide", "x", "urgent", "s", True)


def test_route_links_item():
    res = rt.route_finding("watcher", "w1", "low", "w.py:3", False, item_id="WI-99")
    state = json.loads(rt.LOCAL_STORE.read_text(encoding="utf-8"))
    fp = res.fingerprint
    assert state[fp]["item_id"] == "WI-99"
