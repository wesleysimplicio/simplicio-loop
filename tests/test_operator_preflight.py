from __future__ import annotations

from pathlib import Path

import pytest

from scripts import operator_preflight
from scripts.operator_preflight import DEFAULT_TTL_SECONDS, evaluate, preflight


def test_missing_operator_blocks_without_refresh():
    result = evaluate({}, now=1000, versions={"simplicio-mapper": "missing", "simplicio-dev-cli": "ok"})
    assert result["status"] == "blocked"
    assert result["network_upgrade_allowed"] is False


def test_missing_or_expired_check_requests_refresh():
    result = evaluate({}, now=1000, versions={"simplicio-mapper": "0.23.1", "simplicio-dev-cli": "0.16.1"})
    assert result["status"] == "refresh_required"
    assert result["refresh_required"] is True


def test_fresh_check_stays_offline_within_ttl():
    state = {"checked_at": "1970-01-01T00:15:00Z"}
    result = evaluate(state, now=1000, ttl_seconds=DEFAULT_TTL_SECONDS, versions={"a": "1"})
    assert result["status"] == "cached"
    assert result["network_upgrade_allowed"] is False


def test_run_version_mismatch_warns_without_refreshing_version():
    result = evaluate(
        {"checked_at": "1970-01-01T00:15:00Z"}, now=1000, versions={"a": "2"},
        run_pin={"versions": {"a": "1"}},
    )
    assert result["run_version_mismatch"] is True
    assert "do not upgrade" in result["warning"]


def test_record_persists_check_and_run_pin(tmp_path: Path):
    versions = {"simplicio-mapper": "0.23.1", "simplicio-dev-cli": "0.16.1"}
    result = preflight(
        state_path=tmp_path / "operator-check.json", run_pin_path=tmp_path / "operator-pin.json",
        run_id="run-1", now=1000, record=True, version_provider=lambda: versions,
    )
    assert result["recorded"] is True
    assert (tmp_path / "operator-check.json").exists()
    assert (tmp_path / "operator-pin.json").exists()
    cached = preflight(
        state_path=tmp_path / "operator-check.json", run_pin_path=tmp_path / "operator-pin.json",
        run_id="run-1", now=1001, version_provider=lambda: versions,
    )
    assert cached["status"] == "cached"


def test_invalid_ttl_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError):
        preflight(state_path=tmp_path / "a", run_pin_path=tmp_path / "b", run_id="x", ttl_seconds=0)


def test_installed_versions_classifies_missing_and_available(monkeypatch):
    class Completed:
        stdout = "tool 1.2.3\n"
        stderr = ""

    monkeypatch.setattr(operator_preflight.shutil, "which", lambda name: None if name == "missing" else "tool.exe")
    monkeypatch.setattr(operator_preflight.subprocess, "run", lambda *args, **kwargs: Completed())
    assert operator_preflight.installed_versions(("missing", "present")) == {
        "missing": "missing", "present": "tool 1.2.3"
    }


def test_main_emits_receipt_and_records(tmp_path: Path, capsys):
    code = operator_preflight.main([
        "--state", str(tmp_path / "check.json"),
        "--run-state", str(tmp_path / "pin.json"),
        "--run-id", "cli-run", "--record",
    ])
    assert code == 0
    assert '"schema": "simplicio.operator-preflight/v1"' in capsys.readouterr().out
