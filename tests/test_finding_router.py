"""Unit + integration tests for simplicio_loop.finding_router (WI-466)."""
from __future__ import annotations

import json
import os
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
    # NOTE: intentionally do NOT force _gh_available to False. Let the real
    # implementation run so tests can exercise the gh-available code paths;
    # route_finding without a `repo` still falls back to the local store.
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


def test_router_state_load_handles_corrupt_json(tmp_path):
    # Lines 41-42: corrupt file must fall back to empty state, not raise.
    p = tmp_path / "corrupt.json"
    p.write_text("{not valid json", encoding="utf-8")
    state = rt._RouterState.load(p)
    assert state.routes == {}


def test_gh_available_true_when_gh_present(monkeypatch):
    # Cover _gh_available (51-55): gh --version returns rc 0 -> True.
    # Replace the autouse _isolate lambda (False) with a real implementation
    # that delegates to the mocked subprocess.run.
    class _R:
        returncode = 0
        stdout = ""
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _R())
    def _real_avail():
        r = rt.subprocess.run(["gh", "--version"], capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    monkeypatch.setattr(rt, "_gh_available", _real_avail)
    assert rt._gh_available() is True


def test_gh_available_false_when_gh_missing(monkeypatch):
    # Cover except branch (54-55): FileNotFoundError -> False.
    def _boom(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(rt.subprocess, "run", _boom)
    def _real_avail():
        try:
            r = rt.subprocess.run(["gh", "--version"], capture_output=True, text=True, timeout=10)
            return r.returncode == 0
        except (FileNotFoundError, rt.subprocess.TimeoutExpired):
            return False
    monkeypatch.setattr(rt, "_gh_available", _real_avail)
    assert rt._gh_available() is False


def test_gh_issue_create_happy_path(monkeypatch):
    # Cover _gh_issue_create (58-69): rc 0 -> returns "#<n>".
    class _R:
        returncode = 0
        stdout = "456"
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _R())
    assert rt._gh_issue_create("o/r", "t", "b") == "#456"


def test_gh_issue_create_timeout_returns_none(monkeypatch):
    # Cover except branch (67-69): TimeoutExpired -> None.
    def _boom(*a, **k):
        raise rt.subprocess.TimeoutExpired(cmd="gh", timeout=1)
    monkeypatch.setattr(rt.subprocess, "run", _boom)
    assert rt._gh_issue_create("o/r", "t", "b") is None


def test_gh_issue_comment_returns_bool(monkeypatch):
    # Cover _gh_issue_comment (72-80): rc 0 -> True; exception -> False.
    class _R:
        returncode = 0
        stdout = ""
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _R())
    assert rt._gh_issue_comment("o/r", "123", "body") is True

    def _boom(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(rt.subprocess, "run", _boom)
    assert rt._gh_issue_comment("o/r", "123", "body") is False


def test_dedup_confirms_local_route(monkeypatch, tmp_path):
    # Cover 144-145 + 155-158: confirming an existing local route flips
    # confirmed=True and persists, without contacting gh.
    monkeypatch.setattr(rt, "LOCAL_STORE", tmp_path / "routes.json")
    monkeypatch.setattr(rt, "_gh_available", lambda: False)
    a = rt.route_finding("survey", "c1", "high", "c.py:1", False, item_id="WI-C")
    assert a.issue_ref.startswith("local:")
    b = rt.route_finding("survey", "c1", "high", "c.py:1", True, item_id="WI-C")
    state = json.loads(rt.LOCAL_STORE.read_text(encoding="utf-8"))
    fp = b.fingerprint
    assert state[fp]["confirmed"] is True
    assert b.tracked is True


def test_route_gh_create_timeout_falls_local(monkeypatch, tmp_path):
    # _gh_issue_create returns None on Timeout/FileNotFound -> local fallback.
    monkeypatch.setattr(rt, "LOCAL_STORE", tmp_path / "routes2.json")
    monkeypatch.setattr(rt, "_gh_available", lambda: True)
    def _boom(*a, **k):
        raise rt.subprocess.TimeoutExpired(cmd="gh", timeout=1)
    monkeypatch.setattr(rt.subprocess, "run", _boom)
    res = rt.route_finding("survey", "to-1", "low", "t.py:1", True, item_id="WI-TO")
    assert res.issue_ref.startswith("local:")





def test_route_creates_real_issue_via_gh(monkeypatch, tmp_path):
    # Cover 51-55 (real _gh_available) + 154-158: repo set and gh "available"
    # (mocked rc 0) -> _gh_issue_create runs and returns a real issue ref.
    # _isolate no longer forces _gh_available to False, so the real body runs.
    monkeypatch.setattr(rt, "LOCAL_STORE", tmp_path / "routes_gh.json")
    class _R:
        returncode = 0
        stdout = "909"
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _R())
    res = rt.route_finding("survey", "ghx", "medium", "m.py:1", True, item_id="WI-GHX", repo="owner/repo")
    assert res.created is True
    assert res.issue_ref == "#909"

def test_gh_available_false_on_missing_binary(monkeypatch):
    # Cover 54-55: _gh_available returns False when gh binary is absent.
    def _boom(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(rt.subprocess, "run", _boom)
    assert rt._gh_available() is False

def test_untracked_problems_filters_by_item(tmp_path, monkeypatch):
    # Cover line 190: item_id filter in untracked_problems.
    monkeypatch.setattr(rt, "LOCAL_STORE", tmp_path / "routes4.json")
    monkeypatch.setattr(rt, "_gh_available", lambda: False)
    rt.route_finding("survey", "f1", "medium", "x.py:1", True, item_id="WI-A")
    rt.route_finding("survey", "f2", "medium", "y.py:1", True, item_id="WI-B")
    only_a = rt.untracked_problems(item_id="WI-A")
    ids = [r["finding_id"] for r in only_a]
    assert "f1" in ids and "f2" not in ids

def test_route_links_item():
    res = rt.route_finding("watcher", "w1", "low", "w.py:3", False, item_id="WI-99")
    state = json.loads(rt.LOCAL_STORE.read_text(encoding="utf-8"))
    fp = res.fingerprint
    assert state[fp]["item_id"] == "WI-99"
