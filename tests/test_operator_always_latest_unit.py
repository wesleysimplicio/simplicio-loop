"""Always-latest operator upgrade policy."""
from __future__ import annotations

from pathlib import Path

from scripts import operator_check as oc


def test_always_latest_default_forces_upgrade(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPLICIO_OPERATOR_ALWAYS_LATEST", "1")
    monkeypatch.setattr(oc, "missing_binaries", lambda binaries: [])
    cache = tmp_path / "operator-check.json"
    oc.write_cache(cache, {"last_checked_ts": 1e12, "last_checked_at": "far-future"})
    # No explicit ttl → always-latest applies.
    d = oc.should_upgrade(cache, ttl_days=None)
    assert d["should_upgrade"] is True
    assert d["ttl_days"] == 0.0
    assert "always-latest" in d["reason"]


def test_explicit_ttl_wins_over_always_latest(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPLICIO_OPERATOR_ALWAYS_LATEST", "1")
    monkeypatch.setattr(oc, "missing_binaries", lambda binaries: [])
    cache = tmp_path / "operator-check.json"
    now = 1_000_000.0
    oc.record_check(cache, {"simplicio-mapper": "0.23.1"}, now=now - 3600)
    d = oc.should_upgrade(cache, ttl_days=7, now=now)
    assert d["should_upgrade"] is False
    assert d["ttl_days"] == 7.0


def test_opt_out_restores_legacy_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPLICIO_OPERATOR_ALWAYS_LATEST", "0")
    monkeypatch.setattr(oc, "missing_binaries", lambda binaries: [])
    cache = tmp_path / "operator-check.json"
    now = 1_000_000.0
    oc.record_check(cache, {"simplicio-mapper": "0.23.1"}, now=now - 3600)
    d = oc.should_upgrade(cache, ttl_days=None, now=now)
    assert d["should_upgrade"] is False
    assert d["ttl_days"] == 7.0
